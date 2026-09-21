"""수집기 결과가 들어오는 입구: 할 일 등록 → 알림 게이트."""

import logging
from datetime import datetime

from app.core.events import Event, EventKind, collector_failed
from app.core.interfaces import Collector, GateDecision
from app.scheduler.dispatcher import Dispatcher
from app.storage.todos import TodoRepository

logger = logging.getLogger(__name__)

# 할 일로 자동 등록하는 이벤트 종류 (eClass 과제·시험·강의 기한 등)
TODO_KINDS = frozenset({EventKind.DEADLINE})


class Ingestor:
    def __init__(self, todos: TodoRepository, dispatcher: Dispatcher) -> None:
        self._todos = todos
        self._dispatcher = dispatcher

    async def run_collector(self, collector: Collector, now: datetime) -> list[GateDecision]:
        try:
            events = await collector.collect()
        except Exception as exc:
            # 수집기는 예외를 던지지 않기로 약속했지만, 어겨도 비서 전체가 멈추지 않게 한다.
            # 예외 메시지에는 비밀값이 섞일 수 있어 종류만 남긴다.
            logger.exception("수집기 %s 실행 실패", collector.name)
            events = [collector_failed(collector.name, "unexpected_error", type(exc).__name__)]
        return await self.ingest(events, now)

    async def ingest(self, events: list[Event], now: datetime) -> list[GateDecision]:
        decisions = []
        for event in events:
            try:
                if event.kind == EventKind.SUBMITTED:
                    # 새 소식이 아니라 상태를 맞추는 일이다. 할 일만 닫고 알리지 않는다.
                    await self._close_submitted(event, now)
                    continue
                if event.kind in TODO_KINDS:
                    await self._todos.add_from_event(event, now)
                decisions.append(await self._dispatcher.publish(event, now))
            except Exception:
                logger.exception("이벤트 처리 실패: %s", event.ref_id)
        return decisions

    async def _close_submitted(self, event: Event, now: datetime) -> None:
        """제출이 확인된 과제의 할 일을 완료로 바꾼다. 이미 닫혔거나 없으면 그냥 둔다."""
        ref = str(event.meta.get("todo_ref", ""))
        todo = await self._todos.get_by_ref(ref) if ref else None
        if todo is None or todo.status != "open":
            return
        await self._todos.set_status(todo.id, "done", now)
        logger.info("제출이 확인된 과제 1건을 완료로 바꿨습니다")
