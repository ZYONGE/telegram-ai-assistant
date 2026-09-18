"""기억 도구."""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from app.agent.memory import MarkdownMemoryStore
from app.core.clock import utc_now
from app.core.interfaces import ToolResult
from app.tools.common import SimpleTool, ToolInputError, require_str, spec


def memory_tools(store: MarkdownMemoryStore, clock: Callable[[], datetime] = utc_now) -> list:
    async def remember(args: Mapping[str, Any]) -> ToolResult:
        try:
            item = await store.add(require_str(args, "text", max_len=300), clock())
        except ValueError as exc:
            raise ToolInputError(str(exc)) from exc
        return ToolResult(f"기록했습니다 [{item.item_id}]: {item.text}")

    async def forget(args: Mapping[str, Any]) -> ToolResult:
        item_id = require_str(args, "memory_id", max_len=20)
        if not await store.delete(item_id):
            raise ToolInputError(f"{item_id} 기억이 없습니다. 시스템 프롬프트의 기억 목록에서 ID를 확인하세요.")
        return ToolResult(f"삭제했습니다: {item_id}")

    return [
        SimpleTool(
            spec(
                "remember",
                "사용자에 대한 지속적인 정보(생활 패턴, 목표, 선호, 결정)를 기억에 기록한다. "
                "일회성 내용, 프로필에 이미 있는 내용, 비밀번호·카드·계좌·신분증 번호는 기록하지 않는다.",
                {"text": {"type": "string", "description": "한 문장으로 정리한 사실"}},
                ["text"],
            ),
            remember,
        ),
        SimpleTool(
            spec(
                "forget",
                "기억 하나를 삭제한다. 사용자가 지우라고 하면 바로 사용한다.",
                {"memory_id": {"type": "string", "description": "기억 ID (예: m-1a2b3c)"}},
                ["memory_id"],
            ),
            forget,
        ),
    ]
