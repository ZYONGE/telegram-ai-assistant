"""개발·테스트용 가짜 수집기."""

from collections.abc import Iterable
from datetime import datetime, timedelta

from app.core.events import Event, EventKind


class FakeCollector:
    def __init__(self, events: Iterable[Event], name: str = "fake") -> None:
        self.name = name
        self._events = list(events)

    async def collect(self) -> list[Event]:
        return list(self._events)


def sample_events(now: datetime) -> list[Event]:
    """실행할 때마다 새 ref_id를 만들어 매번 알림 흐름을 확인할 수 있게 한다."""
    stamp = f"{now:%Y%m%d%H%M%S}"
    return [
        Event(
            source="fake",
            kind=EventKind.NOTICE,
            title="[테스트] 자료구조 휴강 안내",
            body="내일 자료구조 수업은 휴강입니다.",
            urgent=True,
            ref_id=f"fake:notice:{stamp}",
        ),
        Event(
            source="fake",
            kind=EventKind.DEADLINE,
            title="[테스트] 알고리즘 과제 2 제출",
            due_at=now + timedelta(days=3),
            ref_id=f"fake:deadline:{stamp}",
        ),
    ]
