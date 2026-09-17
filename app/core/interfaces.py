"""기능끼리 연결되는 공통 접점 (CLAUDE.md 4-1, 5절).

레퍼런스에서 가져온 설계는 여기 정의된 인터페이스의 구현체로만 들어온다.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from app.core.clock import require_aware
from app.core.events import Event

# ---------------------------------------------------------------------------
# 수집기
# ---------------------------------------------------------------------------


@runtime_checkable
class Collector(Protocol):
    """외부 소스(eClass, 학사일정, 채용 페이지 등)에서 새 소식을 가져온다.

    모델과 분리되어 있으며, 실패해도 예외를 밖으로 던지지 않고
    `collector_failed()` 이벤트를 결과에 담아 보고한다.
    """

    name: str

    async def collect(self) -> list[Event]: ...


# ---------------------------------------------------------------------------
# 메일 규칙
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class MailMessage:
    message_id: str
    thread_id: str
    sender: str
    received_at: datetime
    sender_name: str = ""
    subject: str = ""
    # 외부 데이터. 모델에 넘길 때는 잘라서 데이터로만 다룬다.
    snippet: str = ""
    labels: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        require_aware(self.received_at, "MailMessage.received_at")

    @property
    def sender_domain(self) -> str:
        return self.sender.rpartition("@")[2].lower()


class MailAction(StrEnum):
    NOTIFY = "notify"
    MORNING_LIST = "morning_list"
    EVENING_CLEANUP = "evening_cleanup"
    # 규칙 엔진만 실행한다. 실행 직전에 보호 목록(학교·지원 기업)을 코드로 다시 확인한다.
    TRASH = "trash"
    TRACK_REPLY = "track_reply"
    SUGGEST_SCHEDULE = "suggest_schedule"


@dataclass(frozen=True, slots=True)
class RuleResult:
    matched: bool
    actions: tuple[MailAction, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.matched and self.actions:
            raise ValueError("매칭되지 않은 결과에는 후속 동작이 있을 수 없습니다")


NO_MATCH = RuleResult(matched=False)


@runtime_checkable
class Rule(Protocol):
    """사용자가 등록한 메일 유형 하나. 매칭 여부와 후속 동작 목록을 반환한다."""

    name: str

    async def evaluate(self, message: MailMessage) -> RuleResult: ...


# ---------------------------------------------------------------------------
# 기억
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemoryItem:
    item_id: str
    text: str
    created_at: datetime


@runtime_checkable
class MemoryStore(Protocol):
    """비서가 대화 중 기록하는 지속 정보. 사용자가 직접 관리하는 data/profile.md와는 별개다."""

    async def read(self) -> list[MemoryItem]: ...

    async def add(self, text: str) -> MemoryItem: ...

    async def delete(self, item_id: str) -> bool: ...


# ---------------------------------------------------------------------------
# 알림 게이트
# ---------------------------------------------------------------------------


class GateAction(StrEnum):
    SEND_NOW = "send_now"
    BATCH = "batch"
    HOLD = "hold"
    DROP = "drop"


@dataclass(frozen=True, slots=True)
class GateDecision:
    action: GateAction
    reason: str
    # HOLD일 때 다시 판단할 시각
    release_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.action is GateAction.HOLD:
            if self.release_at is None:
                raise ValueError("HOLD 결정에는 release_at이 필요합니다")
            require_aware(self.release_at, "GateDecision.release_at")
        elif self.release_at is not None:
            raise ValueError("release_at은 HOLD 결정에만 쓸 수 있습니다")


@runtime_checkable
class NotificationGate(Protocol):
    """모든 선제 이벤트가 거치는 유일한 관문. 조용한 시간·일일 상한·중복을 코드로 판단한다."""

    async def decide(self, event: Event, now: datetime) -> GateDecision: ...


# ---------------------------------------------------------------------------
# 브리핑
# ---------------------------------------------------------------------------


class BriefingKind(StrEnum):
    MORNING = "morning"
    EVENING = "evening"
    WEEKLY = "weekly"


@dataclass(frozen=True, slots=True)
class BriefingItem:
    section: str
    text: str
    # 클수록 앞에 배치
    priority: int = 0


@runtime_checkable
class BriefingProvider(Protocol):
    """각 기능은 브리핑에 넣을 내용만 제공하고, 조립은 브리핑 모듈 한 곳에서 한다."""

    name: str

    async def briefing_items(self, kind: BriefingKind, now: datetime) -> list[BriefingItem]: ...


# ---------------------------------------------------------------------------
# 출력 채널
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Button:
    label: str
    callback_data: str


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    # 마크다운 서식 없이 보낸다. 4,096자 초과 분할은 채널 구현이 맡는다.
    text: str
    buttons: tuple[Button, ...] = ()


@runtime_checkable
class Notifier(Protocol):
    """사용자 한 명에게 메시지를 보낸다. 선제 알림 경로에서는 알림 게이트만 이것을 호출한다."""

    async def send(self, message: OutgoingMessage) -> None: ...


# ---------------------------------------------------------------------------
# 모델 도구
# ---------------------------------------------------------------------------


class Confirmation(StrEnum):
    # 조회, 할 일 추가·완료·수정, 리마인더·예약 작업, 보관함 저장, 답장 초안 저장
    IMMEDIATE = "immediate"
    # 일정 등록·수정·삭제, 할 일 삭제, 메일 규칙 추가·삭제
    BUTTON = "button"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    confirmation: Confirmation


@dataclass(frozen=True, slots=True)
class ToolResult:
    content: str
    is_error: bool = False


@runtime_checkable
class Tool(Protocol):
    """모델이 쓰는 도구. 도구 레지스트리 한 곳에 등록하고, 확인 단계는 레지스트리가 적용한다."""

    spec: ToolSpec

    async def run(self, args: Mapping[str, Any]) -> ToolResult: ...
