"""모델 호출 약속. 대화 루프·브리핑은 이 약속만 쓰고, 제공사 SDK는 어댑터 안에만 둔다 (docs/adr/0004).

대화 기록은 제공사 형식 그대로 저장하고 그대로 다시 보낸다. 생각 서명 같은 제공사 고유 정보를 잃지 않기 위해서다.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from app.core.interfaces import ToolResult

# 저장·재전송하는 대화 한 턴 (제공사 형식)
Turn = dict[str, Any]


class LLMError(Exception):
    """모델 호출 실패. 제공사 예외를 감싸며, 메시지에 비밀값이나 요청 내용을 넣지 않는다."""


class TransientLLMError(LLMError):
    """잠시 뒤 다시 시도하면 될 수 있는 실패 (서버 오류, 요청 한도, 네트워크)."""


class Finish(StrEnum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    MAX_TOKENS = "max_tokens"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str | None
    name: str
    args: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelTurn:
    # 기록할 모델 응답. 내용이 없으면 None (빈 턴은 다음 요청에서 거절된다)
    content: Turn | None
    text: str
    finish: Finish
    tool_calls: list[ToolCall] = field(default_factory=list)


class ChatModel(Protocol):
    async def generate(
        self, system: str, history: list[Turn], tools: list[dict[str, Any]], *, max_tokens: int
    ) -> ModelTurn:
        """실패하면 LLMError(또는 TransientLLMError)를 던진다."""
        ...

    def user_turn(self, texts: list[str]) -> Turn: ...

    def tool_results_turn(self, results: list[tuple[ToolCall, ToolResult]]) -> Turn: ...

    def pending_tool_calls(self, content: Turn) -> list[ToolCall]:
        """결과 없이 끝난 모델 턴의 도구 호출 (처리 중 중단된 경우 복구용)."""
        ...

    def render(self, role: str, content: Turn) -> list[str]:
        """요약용 대화록 한 턴을 사람이 읽는 줄로 바꾼다."""
        ...


@dataclass(frozen=True, slots=True)
class LLM:
    """설정에서 고른 제공사의 모델 묶음."""

    # 대화·판단
    chat: ChatModel
    # 브리핑 문장 다듬기, 대화 요약
    light: ChatModel
    close: Callable[[], Awaitable[None]]
