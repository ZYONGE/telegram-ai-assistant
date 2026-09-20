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
from datetime import datetime

from app.collectors.eclass.parse import CourseRow
from app.collectors.eclass.session import EclassError, EclassSession, Failure
from app.collectors.eclass.sources import EclassSource, SourceResult, default_sources
from app.core.clock import format_kst, utc_now
from app.core.config import EclassSettings
from app.core.events import Event, collector_failed
from app.storage.eclass import EclassHealthStore, EclassRepository, EclassSourceStateStore

logger = logging.getLogger(__name__)

LAYOUT_MESSAGE = "eClass 화면 구조가 바뀐 것 같습니다. 내용을 읽지 못했습니다."
STALE_MESSAGE = "eClass 확인이 계속 안 되고 있습니다."
BLOCKED_MESSAGE = "로그인이 연속으로 실패해 eClass 자동 확인을 멈췄습니다. 계정을 확인한 뒤 다시 켜 주세요."


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

    async def collect(self) -> list[Event]:
        if not self._settings.enabled:
            return []
        now = self._clock()
        health = await self._health.read()
        if health.login_blocked:
            # 이미 알렸다. 사용자가 계정을 고치고 다시 켤 때까지 건드리지 않는다.
            logger.info("eClass 로그인 실패가 이어져 수집을 건너뜁니다")
            return []

        due = [source for source in self._sources if await self._state.due(source.key, source.interval, now)]
        if not due:
            logger.debug("eClass: 주기가 된 소스가 없습니다")
            return []

        try:
            harvest = await self._run(due)
        except EclassError as exc:
            # 로그인·연결 실패는 소스 이전의 문제다. 아무것도 못 봤으므로 수집기 실패로 본다.
            return await self._failure_events(exc.reason, str(exc), now)

        return await self._events(harvest, now)

    async def _run(self, sources: list[EclassSource]) -> list[Harvest]:
        """세션 하나로 소스를 차례로 돌린다. 소스의 실패는 그 소스에만 남긴다."""
        session = self._session_factory(self._settings)
        await session.start()
        try:
            await session.ensure_login()
            # 과목방마다 따로 있는 화면을 위한 자리. 과목 목록 확보는 T-09에서 붙인다.
            courses: list[CourseRow] = []
            harvest: list[Harvest] = []
            for source in sources:
                harvest.append(await self._run_one(source, session, courses))
            return harvest
        finally:
            await session.close()

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
            for item in entry.result.items:
                seen += 1
                change = await self._items.upsert(item, now)
                event = entry.source.event_for(item, change, now)
                if event is not None:
                    events.append(event)
        # 일부만 실패했다면 수집기는 살아 있다. 실패한 소스만 이름을 붙여 알린다.
        for entry in failed:
            events.append(
                collector_failed(self.name, f"{entry.source.key}:{entry.error.reason}", _detail(entry))
            )
        logger.info("eClass 확인: 소스 %d개, 항목 %d건 중 알릴 것 %d건", len(harvest), seen, len(events))
        return events

    async def _failure_events(self, reason: str, message: str, now: datetime) -> list[Event]:
        await self._health.record_failure(reason, now)
        health = await self._health.read()
        detail = message
        if reason == Failure.LOGIN and health.login_blocked:
            detail = f"{message} {BLOCKED_MESSAGE}"
        events = [collector_failed(self.name, str(reason), detail)]
        if health.stale(now, self._settings.stale_hours):
            events.append(
                collector_failed(
                    self.name,
                    "stale",
                    f"{STALE_MESSAGE} 마지막 확인 {format_kst(health.last_ok_at)}",
                )
            )
        return events


def _detail(entry: Harvest) -> str:
    """사용자에게 보일 실패 설명. 어느 화면이 막혔는지 밝힌다."""
    return f"{entry.source.label}: {entry.error}"
