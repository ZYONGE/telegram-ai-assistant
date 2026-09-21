"""eClass 수집기: 로그인 → 켜진 소스를 차례로 → 이벤트.

- 모델을 부르지 않는다. 화면을 읽는 일은 코드로만 한다.
- 화면마다 소스 하나를 둔다 (`sources/`). 이 파일은 소스를 돌리고 결과를 모으는 역할만 한다.
- **한 소스가 실패해도 나머지는 계속 돈다.** 실패한 소스만 이름을 붙여 보고한다.
- 실패하면 예외를 밖으로 던지지 않고 `collector_failed` 이벤트로 보고한다. 같은 원인은 게이트가 한 번만 알린다.
- 로그인 연속 2회 실패면 자동화를 멈춘다. 사용자가 계정을 고치고 다시 켜야 한다 (CLAUDE.md 6절).
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.collectors.eclass.parse import CourseRow, parse_course_select
from app.collectors.eclass.session import TODO_PATH, EclassError, EclassSession, Failure
from app.collectors.eclass.sources import EclassSource, SourceResult, default_sources
from app.core.clock import format_kst, utc_now
from app.core.config import EclassSettings
from app.core.events import Event, collector_failed
from app.storage.eclass import (
    CollectorHealth,
    EclassHealthStore,
    EclassRepository,
    EclassSourceStateStore,
)

logger = logging.getLogger(__name__)

LAYOUT_MESSAGE = "eClass 화면 구조가 바뀐 것 같습니다. 내용을 읽지 못했습니다."
STALE_MESSAGE = "eClass 확인이 계속 안 되고 있습니다."
BLOCKED_MESSAGE = "로그인이 연속으로 실패해 eClass 자동 확인을 멈췄습니다. 계정을 확인한 뒤 다시 켜 주세요."
# 한 번에 돌릴 소스 수. 남은 소스는 다음 차례에 먼저 돈다.
MAX_SOURCES_PER_RUN = 8
# 그중 과목방 화면은 이만큼만. 과목마다 문을 열고 들어가야 해서 한 소스가 수십 번 요청한다.
MAX_COURSE_SOURCES_PER_RUN = 2
# 화면을 처음 볼 때는 거기 쌓여 있던 지난 글까지 전부 새 글이다.
# 이만큼 안에 올라온 글만 알리고 나머지는 조용히 담는다.
FIRST_RUN_WINDOW = timedelta(days=7)
# 연결 실패는 이만큼 이어져야 알린다. 기기를 옮길 때 잠깐 끊기는 것까지 알리지 않는다.
MIN_NETWORK_FAILURES = 2
# 한 번도 돌지 않은 소스는 가장 오래 기다린 것으로 본다
NEVER = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Harvest:
    """소스 하나를 돌린 결과. 성공이면 result, 실패면 error가 찬다."""

    source: EclassSource
    result: SourceResult | None = None
    error: EclassError | None = None


class EclassCollector:
    name = "eclass"

    def __init__(
        self,
        settings: EclassSettings,
        items: EclassRepository,
        health: EclassHealthStore,
        state: EclassSourceStateStore,
        clock: Callable[[], datetime] = utc_now,
        session_factory: Callable[[EclassSettings], object] = EclassSession,
        sources: Sequence[EclassSource] | None = None,
    ) -> None:
        self._settings = settings
        self._items = items
        self._health = health
        self._state = state
        self._clock = clock
        self._session_factory = session_factory
        self._sources = list(sources) if sources is not None else default_sources()

    def set_sources(self, sources: Sequence[EclassSource]) -> None:
        """탐색 결과로 만든 소스로 바꾼다. 비서를 켤 때 한 번 부른다."""
        self._sources = list(sources)

    async def collect(self) -> list[Event]:
        if not self._settings.enabled:
            return []
        now = self._clock()
        health = await self._health.read()
        if health.login_blocked:
            # 이미 알렸다. 사용자가 계정을 고치고 다시 켤 때까지 건드리지 않는다.
            logger.info("eClass 로그인 실패가 이어져 수집을 건너뜁니다")
            return []

        due = await self._due(now)
        if not due:
            logger.debug("eClass: 주기가 된 소스가 없습니다")
            return []

        try:
            harvest = await self._run(due)
        except EclassError as exc:
            # 로그인·연결 실패는 소스 이전의 문제다. 아무것도 못 봤으므로 수집기 실패로 본다.
            return await self._failure_events(exc.reason, str(exc), now)

        return await self._events(harvest, now)

    async def _due(self, now: datetime) -> list[EclassSource]:
        """이번에 돌릴 소스. **오래 기다린 것부터** 보고, 같은 때면 알릴 화면을 앞에 둔다.

        기다린 순으로 보지 않으면 늘 같은 화면만 돌고 나머지는 영영 밀린다
        (알릴 화면은 주기가 0이라 언제나 차례가 돌아온다).
        """
        waiting: list[tuple[int, datetime, EclassSource]] = []
        for source in self._sources:
            if not await self._state.due(source.key, source.interval, now):
                continue
            state = await self._state.read(source.key)
            last_run = state.last_run_at if state is not None else NEVER
            waiting.append((getattr(source, "priority", 1), last_run, source))
        waiting.sort(key=lambda row: (row[1], row[0]))

        picked: list[EclassSource] = []
        rooms = 0
        for _priority, _last_run, source in waiting:
            if len(picked) >= MAX_SOURCES_PER_RUN:
                break
            if getattr(source, "per_course", False):
                if rooms >= MAX_COURSE_SOURCES_PER_RUN:
                    continue
                rooms += 1
            picked.append(source)
        return picked

    async def _run(self, sources: list[EclassSource]) -> list[Harvest]:
        """세션 하나로 소스를 차례로 돌린다. 소스의 실패는 그 소스에만 남긴다."""
        session = self._session_factory(self._settings)
        await session.start()
        try:
            await session.ensure_login()
            courses = await self._courses(session, sources)
            harvest: list[Harvest] = []
            for source in sources:
                harvest.append(await self._run_one(source, session, courses))
            return harvest
        finally:
            await session.close()

    async def _courses(self, session: object, sources: list[EclassSource]) -> list[CourseRow]:
        """과목방 화면을 볼 소스가 있을 때만 수강 과목을 읽는다."""
        if not any(getattr(source, "per_course", False) for source in sources):
            return []
        try:
            rows = parse_course_select(await session.open(TODO_PATH)).rows
        except EclassError as exc:
            logger.warning("수강 과목을 읽지 못했습니다 (%s)", exc.reason)
            return []
        logger.info("수강 과목 %d개", len(rows))
        return rows

    async def _run_one(self, source: EclassSource, session: object, courses: list[CourseRow]) -> Harvest:
        try:
            result = await source.fetch(session, courses)
        except EclassError as exc:
            logger.warning("eClass %s 수집 실패: %s", source.key, exc.reason)
            return Harvest(source, error=exc)
        if result.suspicious:
            return Harvest(source, error=EclassError(Failure.LAYOUT, LAYOUT_MESSAGE))
        return Harvest(source, result=result)

    async def _events(self, harvest: list[Harvest], now: datetime) -> list[Event]:
        # 기록하기 전에 봐야 한다. 기록하고 나면 처음인지 알 수 없다.
        first_run = {
            entry.source.key for entry in harvest if await self._state.read(entry.source.key) is None
        }
        for entry in harvest:
            await self._state.record_run(
                entry.source.key, now, ok=entry.error is None, reason=str(entry.error.reason) if entry.error else ""
            )

        failed = [entry for entry in harvest if entry.error is not None]
        succeeded = [entry for entry in harvest if entry.error is None]
        if not succeeded:
            # 돌린 소스가 전부 실패했다. 수집기 자체가 막힌 것으로 보고 연속 실패를 센다.
            first = failed[0]
            return await self._failure_events(first.error.reason, _detail(first), now)

        await self._health.record_success(now)
        events: list[Event] = []
        seen = 0
        for entry in succeeded:
            backfill = entry.source.key in first_run
            for item in entry.result.items:
                seen += 1
                change = await self._items.upsert(item, now)
                if backfill and _is_old(item, now):
                    continue  # 처음 보는 화면에 쌓여 있던 지난 글
                event = entry.source.event_for(item, change, now)
                if event is not None:
                    events.append(event)
            # 항목 변화와 상관없는 신호 (과제 제출 확인). 알리지 않고 할 일만 닫는다.
            events.extend(entry.result.events)
        # 일부만 실패했다면 수집기는 살아 있다. 실패한 소스만 이름을 붙여 알린다.
        for entry in failed:
            events.append(
                collector_failed(self.name, f"{entry.source.key}:{entry.error.reason}", _detail(entry))
            )
        logger.info("eClass 확인: 소스 %d개, 항목 %d건 중 알릴 것 %d건", len(harvest), seen, len(events))
        return events

    async def _failure_events(self, reason: str, message: str, now: datetime) -> list[Event]:
        count = await self._health.record_failure(reason, now)
        health = await self._health.read()

        # 기기를 들고 다니면 인터넷이 잠깐씩 끊긴다. 그때마다 알리면 성가시다.
        # 연결 문제는 이어질 때만 알린다. 오래 끊긴 것은 아래 stale이 따로 잡는다.
        if reason == Failure.NETWORK and count < MIN_NETWORK_FAILURES:
            logger.info("eClass 연결 실패 %d회. 이어지면 알린다", count)
            return self._stale_events(health, now)

        detail = message
        if reason == Failure.LOGIN and health.login_blocked:
            detail = f"{message} {BLOCKED_MESSAGE}"
        return [collector_failed(self.name, str(reason), detail), *self._stale_events(health, now)]

    def _stale_events(self, health: CollectorHealth, now: datetime) -> list[Event]:
        """오래 확인하지 못했을 때. 잠깐 끊긴 것과 달리 이건 사람이 알아야 한다."""
        if not health.stale(now, self._settings.stale_hours):
            return []
        return [
            collector_failed(
                self.name,
                "stale",
                f"{STALE_MESSAGE} 마지막 확인 {format_kst(health.last_ok_at)}",
            )
        ]


def _is_old(item, now: datetime) -> bool:
    """올린 시각이 오래된 글인지. 시각을 모르는 것(할 일 같은 것)은 오래됐다고 보지 않는다."""
    return item.posted_at is not None and now - item.posted_at > FIRST_RUN_WINDOW


def _detail(entry: Harvest) -> str:
    """사용자에게 보일 실패 설명. 어느 화면이 막혔는지 밝힌다."""
    return f"{entry.source.label}: {entry.error}"
