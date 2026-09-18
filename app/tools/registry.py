"""도구 레지스트리: 모델이 쓰는 모든 도구를 한 곳에 등록하고 확인 단계를 적용한다 (CLAUDE.md 7절).

- Confirmation.IMMEDIATE: 바로 실행
- Confirmation.BUTTON: 실행하지 않고 확인 대기로 저장한 뒤, 사용자가 [확인]을 누르면 실행
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.interfaces import Confirmation, Tool, ToolResult
from app.storage.conversation import PendingAction, PendingActionStore
from app.tools.common import ToolInputError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    result: ToolResult
    # 확인 버튼이 필요한 호출이면 채워진다
    pending: PendingAction | None = None


class ToolRegistry:
    def __init__(self, pending: PendingActionStore) -> None:
        self._pending = pending
        self._tools: dict[str, Tool] = {}

    def register(self, *tools: Tool) -> None:
        for tool in tools:
            if tool.spec.name in self._tools:
                raise ValueError(f"도구 이름이 중복되었습니다: {tool.spec.name}")
            self._tools[tool.spec.name] = tool

    def definitions(self, *, immediate_only: bool = False) -> list[dict[str, Any]]:
        """API에 넘길 도구 정의. 프롬프트 캐시가 유지되도록 이름순으로 고정한다."""
        return [
            {"name": tool.spec.name, "description": tool.spec.description, "input_schema": dict(tool.spec.input_schema)}
            for name, tool in sorted(self._tools.items())
            if not immediate_only or tool.spec.confirmation is Confirmation.IMMEDIATE
        ]

    async def call(self, name: str, args: Mapping[str, Any], now: datetime) -> ToolOutcome:
        tool = self._tools.get(name)
        if tool is None:
            return ToolOutcome(ToolResult(f"알 수 없는 도구입니다: {name}", is_error=True))
        if tool.spec.confirmation is Confirmation.BUTTON:
            return await self._request_confirmation(tool, args, now)
        return ToolOutcome(await _run(tool, args))

    async def confirm(self, action_id: str, now: datetime) -> tuple[PendingAction, ToolResult] | None:
        """확인 버튼 처리. 이미 처리된 요청이면 None."""
        action = await self._pending.get(action_id)
        if action is None or not await self._pending.resolve(action_id, "done", now):
            return None
        tool = self._tools.get(action.tool_name)
        if tool is None:
            return action, ToolResult(f"알 수 없는 도구입니다: {action.tool_name}", is_error=True)
        return action, await _run(tool, action.args)

    async def cancel(self, action_id: str, now: datetime) -> PendingAction | None:
        action = await self._pending.get(action_id)
        if action is None or not await self._pending.resolve(action_id, "cancelled", now):
            return None
        return action

    async def _request_confirmation(self, tool: Tool, args: Mapping[str, Any], now: datetime) -> ToolOutcome:
        # 확인 버튼 문구를 만드는 선택 기능. 대상이 없으면 여기서 오류를 돌려준다.
        describe = getattr(tool, "describe", None)
        try:
            summary = await describe(args) if describe else tool.spec.name
        except ToolInputError as exc:
            return ToolOutcome(ToolResult(str(exc), is_error=True))
        action = await self._pending.create(tool.spec.name, dict(args), summary, now)
        return ToolOutcome(
            ToolResult(f"확인 요청을 보냈습니다: {summary}\n사용자가 [확인]을 누르면 실행됩니다. 아직 실행되지 않았습니다."),
            pending=action,
        )


async def _run(tool: Tool, args: Mapping[str, Any]) -> ToolResult:
    try:
        return await tool.run(args)
    except ToolInputError as exc:
        return ToolResult(str(exc), is_error=True)
    except Exception:
        logger.exception("도구 실행 실패: %s", tool.spec.name)
        return ToolResult("도구 실행 중 오류가 발생했습니다.", is_error=True)
