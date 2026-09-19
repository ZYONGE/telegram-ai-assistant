"""eClass 수집기: 로그인 → 할 일 목록 → 이벤트.

- 모델을 부르지 않는다. 과제·시험·온라인 강의 기한을 코드로만 읽는다.
- 새 항목은 할 일로 자동 등록되도록 `Event(kind=deadline)`로 올린다 (등록은 Ingestor가 한다).
- 마감이 바뀌면 한 번만 알린다. 이전 마감 값은 저장소가 기억한다.
- 실패하면 예외를 밖으로 던지지 않고 `collector_failed` 이벤트로 보고한다. 같은 원인은 게이트가 한 번만 알린다.
- 로그인 연속 2회 실패면 자동화를 멈춘다. 사용자가 계정을 고치고 다시 켜야 한다 (CLAUDE.md 6절).
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from app.collectors.eclass.parse import TodoRow, parse_todo_list
from app.collectors.eclass.session import MAIN_PATH, TODO_PATH, EclassError, EclassSession, Failure
from app.core.clock import format_kst, utc_now
from app.core.config import EclassSettings
from app.core.events import Event, EventKind, EventSource, collector_failed
from app.storage.eclass import EclassHealthStore, EclassItem, EclassRepository, ItemChange

logger = logging.getLogger(__name__)

# 할 일 목록은 두 번에 나눠 받는다: 껍데기(과목 선택)를 먼저 열고, 그 안에서 목록을 부른다.
TODO_ROWS_PATH = "/ilos/mp/todo_list.acl"
TODO_FORM_DATA = {"TODO_CNT": "100", "encoding": "utf-8"}
TODO_ROWS_DATA = {"todoKjList": "", "chk_cate": "ALL", "encoding": "utf-8"}

# 이 시간 안에 닥친 마감은 즉시 알린다
URGENT_WINDOW = timedelta(hours=24)
LAYOUT_MESSAGE = "eClass 화면 구조가 바뀐 것 같습니다. 할 일 목록을 읽지 못했습니다."
STALE_MESSAGE = "eClass 확인이 계속 안 되고 있습니다."
BLOCKED_MESSAGE = "로그인이 연속으로 실패해 eClass 자동 확인을 멈췄습니다. 계정을 확인한 뒤 다시 켜 주세요."


class EclassCollector:
    name = "eclass"

    def __init__(
        self,
        settings: EclassSettings,
        items: EclassRepository,
        health: EclassHealthStore,
        clock: Callable[[], datetime] = utc_now,
        session_factory: Callable[[EclassSettings], object] = EclassSession,
    ) -> None:
        self._settings = settings
        self._items = items
        self._health = health
        self._clock = clock
        self._session_factory = session_factory

    async def collect(self) -> list[Event]:
        if not self._settings.enabled:
            return []
        now = self._clock()
        health = await self._health.read()
        if health.login_blocked:
            # 이미 알렸다. 사용자가 계정을 고치고 다시 켤 때까지 건드리지 않는다.
            logger.info("eClass 로그인 실패가 이어져 수집을 건너뜁니다")
            return []

        try:
            rows, suspicious = await self._fetch()
        except EclassError as exc:
            return await self._failure_events(exc.reason, str(exc), now)

        if suspicious:
            return await self._failure_events(Failure.LAYOUT, LAYOUT_MESSAGE, now)

        await self._health.record_success(now)
        events: list[Event] = []
        for row in rows:
            change = await self._items.upsert(_to_item(row), now)
            event = self._event_for(row, change, now)
            if event is not None:
                events.append(event)
        logger.info("eClass 확인: %d건 중 알릴 것 %d건", len(rows), len(events))
        return events

    async def _fetch(self) -> tuple[list[TodoRow], bool]:
        session = self._session_factory(self._settings)
        await session.start()
        try:
            await session.ensure_login()
            # 할 일 껍데기를 먼저 열어야 그 안의 목록 요청이 받아들여진다
            await session.open(MAIN_PATH)
            await session.post(TODO_PATH, dict(TODO_FORM_DATA))
            html = await session.post(TODO_ROWS_PATH, dict(TODO_ROWS_DATA))
        finally:
            await session.close()
        result = parse_todo_list(html)
        return result.rows, result.suspicious

    def _event_for(self, row: TodoRow, change: ItemChange, now: datetime) -> Event | None:
        if change is ItemChange.SAME:
            return None
        title = f"[{row.course}] {row.title}" if row.course else row.title
        urgent = bool(row.due_at and row.due_at - now <= URGENT_WINDOW)

        if change is ItemChange.DUE_CHANGED:
            return Event(
                source=EventSource.ECLASS,
                kind=EventKind.DEADLINE_CHANGED,
                title=f"마감이 바뀌었습니다: {title}",
                body=f"{row.category} · 새 마감 {format_kst(row.due_at)}" if row.due_at else f"{row.category} · 마감 없음",
                urgent=True,
                due_at=row.due_at,
                ref_id=f"{row.item_id}:due:{row.due_at.isoformat() if row.due_at else 'none'}",
                meta={"course": row.course, "category": row.category},
            )

        kind = EventKind.DEADLINE if row.due_at else EventKind.NOTICE
        return Event(
            source=EventSource.ECLASS,
            kind=kind,
            title=title,
            body=row.category,
            urgent=urgent,
            due_at=row.due_at,
            ref_id=row.item_id,
            meta={"course": row.course, "category": row.category},
        )

    async def _failure_events(self, reason: str, message: str, now: datetime) -> list[Event]:
        count = await self._health.record_failure(reason, now)
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


def _to_item(row: TodoRow) -> EclassItem:
    return EclassItem(
        item_id=row.item_id,
        kind=row.category,
        title=row.title,
        course=row.course,
        due_at=row.due_at,
    )
