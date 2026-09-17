"""모든 기능이 주고받는 공통 이벤트.

외부 변화(eClass 새 글, 메일, 채용 공고, 예약 시각 도달 등)는 모두 Event로 표현하고,
선제 알림은 반드시 알림 게이트를 거친다.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.core.clock import require_aware


class EventSource(StrEnum):
    ECLASS = "eclass"
    GMAIL = "gmail"
    CALENDAR = "calendar"
    SCHEDULER = "scheduler"
    WEATHER = "weather"
    JOB_BOARD = "job_board"
    ACADEMIC = "academic"
    SYSTEM = "system"


class EventKind(StrEnum):
    REMINDER = "reminder"
    DEADLINE = "deadline"
    DEADLINE_CHANGED = "deadline_changed"
    NOTICE = "notice"
    MAIL = "mail"
    SCHEDULE_CONFLICT = "schedule_conflict"
    JOB_POSTING = "job_posting"
    BRIEFING = "briefing"
    COLLECTOR_FAILED = "collector_failed"


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    source: str
    kind: str
    title: str
    ref_id: str
    body: str = ""
    urgent: bool = False
    # 사용자가 직접 그 시각으로 요청한 알림. 조용한 시간과 일일 상한의 예외가 된다.
    user_requested: bool = False
    due_at: datetime | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("source", "kind", "title", "ref_id"):
            if not getattr(self, name):
                raise ValueError(f"Event.{name}은 비어 있을 수 없습니다")
        if self.due_at is not None:
            require_aware(self.due_at, "Event.due_at")


def collector_failed(source: str, reason: str, detail: str = "") -> Event:
    """수집·동기화 실패 보고. 같은 원인의 반복 실패는 ref_id가 같아 중복으로 걸러진다."""
    return Event(
        source=source,
        kind=EventKind.COLLECTOR_FAILED,
        title=f"{source} 확인이 안 되고 있습니다",
        body=detail,
        ref_id=f"{source}:{EventKind.COLLECTOR_FAILED}:{reason}",
        meta={"reason": reason},
    )
