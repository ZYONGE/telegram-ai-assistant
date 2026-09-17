"""대화 루프: 사용자 메시지 → 모델 → 도구 → 답변.

- 대화 기록은 SQLite에 추가만 한다. 모델 응답 content 블록(생각 블록 포함)을 그대로 저장하고 그대로 다시 보낸다.
- 유휴 시간이 지나거나 기록이 길어지면 가벼운 모델로 요약하고, 요약을 담은 새 대화로 이어 간다 (nanobot 참고, docs/adr/0003).
- 도구 확인 단계는 ToolRegistry가 적용한다.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import anthropic

from app.agent.light import LightModel
from app.agent.prompt import PromptBuilder, format_now
from app.core.config import ConversationSettings
from app.storage.conversation import ConversationStore, PendingAction
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 8
MAX_OUTPUT_TOKENS = 16000
REFUSAL_REPLY = "그 요청은 도와드리기 어렵습니다."
TOO_MANY_STEPS_REPLY = "처리할 단계가 너무 많아 여기서 멈췄습니다. 요청을 나눠서 말씀해 주세요."


@dataclass(slots=True)
class AssistantReply:
    text: str
    confirmations: list[PendingAction] = field(default_factory=list)


class Assistant:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        model: str,
        settings: ConversationSettings,
        prompt: PromptBuilder,
        store: ConversationStore,
        registry: ToolRegistry,
        light: LightModel,
    ) -> None:
        self._client = client
        self._model = model
        self._settings = settings
        self._prompt = prompt
        self._store = store
        self._registry = registry
        self._light = light
        self._lock = asyncio.Lock()

    async def reply(self, text: str, now: datetime) -> AssistantReply:
        async with self._lock:
            if await self._store.count_active() >= self._settings.max_active_messages:
                await self._try_compact(now)

            content = [{"type": "text", "text": f"[현재 시각] {format_now(now)}"}]
            content += [{"type": "text", "text": f"[알림] {note}"} for note in await self._store.consume_notes()]
            content.append({"type": "text", "text": text})
            await self._store.append("user", content, now)

            messages = [{"role": m.role, "content": m.content} for m in await self._store.active_messages(now)]
            system = await self._prompt.build(await self._store.summary())
            return await self._run(system, messages, self._registry.definitions(), now, persist=True)

    async def run_task(self, instruction: str, now: datetime) -> str:
        """예약 작업 실행. 대화 기록과 분리된 1회성 실행이며, 확인 버튼이 필요한 도구는 주지 않는다."""
        system = await self._prompt.build(await self._store.summary())
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"[현재 시각] {format_now(now)}"},
                    {
                        "type": "text",
                        "text": "[예약 작업] 사용자님이 미리 예약해 둔 작업입니다. 수행한 뒤 결과를 사용자님께 보낼 메시지로 작성하세요.\n"
                        + instruction,
                    },
                ],
            }
        ]
        reply = await self._run(system, messages, self._registry.definitions(immediate_only=True), now, persist=False)
        return reply.text

    async def compact_if_idle(self, now: datetime) -> bool:
        if self._lock.locked():
            return False
        last = await self._store.last_activity()
        if last is None or now - last < timedelta(minutes=self._settings.idle_compact_minutes):
            return False
        async with self._lock:
            return await self._try_compact(now)

    async def _try_compact(self, now: datetime) -> bool:
        messages = await self._store.active_messages(now)
        if not messages:
            return False
        try:
            summary = await self._light.summarize(await self._store.summary(), messages)
        except Exception:
            logger.exception("대화 요약 실패. 원본 대화를 유지합니다")
            return False
        await self._store.archive_with_summary(messages[-1].id, summary, now)
        logger.info("대화 %d건을 요약으로 압축했습니다", len(messages))
        return True

    async def _run(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], now: datetime, *, persist: bool
    ) -> AssistantReply:
        confirmations: list[PendingAction] = []
        for _ in range(MAX_TOOL_ROUNDS):
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system,
                messages=messages,
                cache_control={"type": "ephemeral"},
                **({"tools": tools} if tools else {}),
            )
            content = [block.model_dump(mode="json", exclude_none=True) for block in response.content]
            # 거부 응답처럼 내용이 비면 기록하지 않는다 (빈 assistant 메시지는 다음 요청에서 거절된다)
            if content:
                await self._record(messages, "assistant", content, now, persist)

            if response.stop_reason != "tool_use":
                return AssistantReply(final_text(response), confirmations)

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                outcome = await self._registry.call(block.name, block.input, now)
                if outcome.pending is not None:
                    confirmations.append(outcome.pending)
                result: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": outcome.result.content,
                }
                if outcome.result.is_error:
                    result["is_error"] = True
                results.append(result)
            await self._record(messages, "user", results, now, persist)

        return AssistantReply(TOO_MANY_STEPS_REPLY, confirmations)

    async def _record(
        self, messages: list[dict[str, Any]], role: str, content: list[dict[str, Any]], now: datetime, persist: bool
    ) -> None:
        messages.append({"role": role, "content": content})
        if persist:
            await self._store.append(role, content, now)


def final_text(response: Any) -> str:
    if response.stop_reason == "refusal":
        return REFUSAL_REPLY
    text = strip_markdown("\n".join(b.text for b in response.content if b.type == "text").strip())
    if response.stop_reason == "max_tokens":
        text += "\n(답변이 길어 중간에 끊겼습니다.)"
    return text or "처리했습니다."


_BOLD = re.compile(r"(\*\*|__)(.+?)\1")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_BULLET = re.compile(r"^(\s*)[-*]\s+", re.MULTILINE)


def strip_markdown(text: str) -> str:
    """텔레그램은 평문으로 보내므로 모델이 섞은 마크다운 기호를 걷어 낸다."""
    text = _BOLD.sub(r"\2", text)
    text = _HEADING.sub("", text)
    text = _BULLET.sub(r"\1· ", text)
    return text.replace("`", "")
