"""대화 루프: 사용자 메시지 → 모델 → 도구 → 답변.

- 대화 기록은 SQLite에 추가만 한다. 모델 응답은 제공자 형식 그대로 저장하고 그대로 다시 보낸다.
- 유휴 시간이 지나거나 기록이 길어지면 가벼운 모델로 요약하고, 요약을 담은 새 대화로 이어 간다 (nanobot 참고, docs/adr/0003).
- 도구 확인 단계는 ToolRegistry가 적용한다. 모델 제공사별 형식은 app/llm 어댑터가 맡는다 (docs/adr/0004).
- 도구를 쓰는 요청이면 끝나기 전에 "확인하고 말씀드릴게요" 같은 한 줄을 먼저 보낸다 (사용자 지시 2026-09-21).
  모델이 도구를 부르며 함께 쓴 문장을 보내고, 없으면 기본 문장을 보낸다. 한 요청에 한 번만.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections.abc import Awaitable, Callable
from typing import Any

from app.agent.light import LightModel
from app.agent.prompt import PromptBuilder, format_now
from app.core.config import ConversationSettings
from app.core.interfaces import ToolResult
from app.llm import ChatModel, Finish, ModelTurn, ToolCall, Turn
from app.storage.conversation import ConversationStore, PendingAction
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 8
MAX_OUTPUT_TOKENS = 16000
BLOCKED_REPLY = "그 요청은 도와드리기 어렵습니다."
TOO_MANY_STEPS_REPLY = "처리할 단계가 너무 많아 여기서 멈췄습니다. 요청을 나눠서 말씀해 주세요."
INTERRUPTED_RESULT = "이 도구 실행은 중단되어 결과가 없습니다."
# 도구를 쓰기 시작할 때 먼저 보내는 한 줄. 모델이 쓴 문장이 없을 때만 쓴다.
DEFAULT_ACK = "확인하고 말씀드릴게요."

# 처리 중에 먼저 보낼 말을 받는 곳 (텔레그램 채널이 채운다)
Progress = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class AssistantReply:
    text: str
    confirmations: list[PendingAction] = field(default_factory=list)


class Assistant:
    def __init__(
        self,
        model: ChatModel,
        settings: ConversationSettings,
        prompt: PromptBuilder,
        store: ConversationStore,
        registry: ToolRegistry,
        light: LightModel,
    ) -> None:
        self._model = model
        self._settings = settings
        self._prompt = prompt
        self._store = store
        self._registry = registry
        self._light = light
        self._lock = asyncio.Lock()

    async def reply(self, text: str, now: datetime, progress: Progress | None = None) -> AssistantReply:
        async with self._lock:
            if await self._store.count_active() >= self._settings.max_active_messages:
                await self._try_compact(now)
            await self._close_interrupted_tool_calls(now)

            texts = [f"[현재 시각] {format_now(now)}"]
            texts += [f"[알림] {note}" for note in await self._store.consume_notes()]
            texts.append(text)
            await self._store.append("user", self._model.user_turn(texts), now)

            history = [m.content for m in await self._store.active_messages()]
            system = await self._prompt.build(await self._store.summary())
            return await self._run(
                system, history, self._registry.definitions(), now, persist=True, progress=progress
            )

    async def run_task(self, instruction: str, now: datetime) -> str:
        """예약 작업 실행. 대화 기록과 분리된 1회성 실행이며, 확인 버튼이 필요한 도구는 주지 않는다."""
        system = await self._prompt.build(await self._store.summary())
        history = [
            self._model.user_turn(
                [
                    f"[현재 시각] {format_now(now)}",
                    "[예약 작업] 사용자가 미리 예약해 둔 작업입니다. 수행한 뒤 결과를 사용자에게 보낼 메시지로 작성하세요.\n"
                    + instruction,
                ]
            )
        ]
        reply = await self._run(system, history, self._registry.definitions(immediate_only=True), now, persist=False)
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
        messages = await self._store.active_messages()
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

    async def _close_interrupted_tool_calls(self, now: datetime) -> None:
        """도구 호출 뒤에 결과가 없으면(처리 중 중단) 오류 결과를 채워 대화 형식을 맞춘다."""
        messages = await self._store.active_messages()
        if not messages or messages[-1].role != "assistant":
            return
        calls = self._model.pending_tool_calls(messages[-1].content)
        if calls:
            failed = ToolResult(INTERRUPTED_RESULT, is_error=True)
            await self._store.append("user", self._model.tool_results_turn([(c, failed) for c in calls]), now)

    async def _run(
        self,
        system: str,
        history: list[Turn],
        tools: list[dict[str, Any]],
        now: datetime,
        *,
        persist: bool,
        progress: Progress | None = None,
    ) -> AssistantReply:
        confirmations: list[PendingAction] = []
        acknowledged = progress is None
        for _ in range(MAX_TOOL_ROUNDS):
            turn = await self._model.generate(system, history, tools, max_tokens=MAX_OUTPUT_TOKENS)
            if turn.content is not None:
                await self._record(history, "assistant", turn.content, now, persist)

            if turn.finish is not Finish.TOOL_CALLS:
                return AssistantReply(final_text(turn), confirmations)

            if not acknowledged:
                acknowledged = True
                await _send_progress(progress, strip_markdown(turn.text.strip()) or DEFAULT_ACK)

            results: list[tuple[ToolCall, ToolResult]] = []
            for call in turn.tool_calls:
                outcome = await self._registry.call(call.name, call.args, now)
                if outcome.pending is not None:
                    confirmations.append(outcome.pending)
                results.append((call, outcome.result))
            await self._record(history, "user", self._model.tool_results_turn(results), now, persist)

        return AssistantReply(TOO_MANY_STEPS_REPLY, confirmations)

    async def _record(self, history: list[Turn], role: str, content: Turn, now: datetime, persist: bool) -> None:
        history.append(content)
        if persist:
            await self._store.append(role, content, now)


async def _send_progress(progress: Progress, text: str) -> None:
    """먼저 보내는 한 줄. 못 보내도 요청 처리는 계속한다."""
    try:
        await progress(text)
    except Exception:
        logger.exception("처리 중 안내를 보내지 못했습니다")


def final_text(turn: ModelTurn) -> str:
    if turn.finish is Finish.BLOCKED:
        return BLOCKED_REPLY
    text = strip_markdown(turn.text.strip())
    if turn.finish is Finish.MAX_TOKENS:
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
