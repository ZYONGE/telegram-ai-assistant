"""마감 리마인더: 할 일의 마감 전 정해 둔 시점마다 한 번씩 알린다.

시점은 설정(`[notification] deadline_reminders`)에서 정한다. 사용자가 지시 파일에 적은 값이다
(2026-09-21: 7일·4일·1일·12시간·3시간·1시간 전, 끝내지 않았으면 계속).

- 끝낸 할 일은 알리지 않는다. eClass에서 제출이 확인되면 할 일이 닫혀 그다음부터 조용해진다.
- 한 시점에 한 번. 같은 할 일·같은 마감·같은 시점은 ref_id가 같아 게이트가 두 번 보내지 않는다.
  마감이 바뀌면 ref_id도 바뀌어 새 마감을 기준으로 다시 센다.
- 비서가 꺼져 있다 켜져 여러 시점을 한꺼번에 지나쳤으면 **가장 가까운 시점 하나만** 알린다.
- 할 일을 만들 때 이미 지나 있던 시점은 알리지 않는다. 방금 "내일까지"라고 적은 일을 곧바로 재촉하지 않는다.
- 조용한 시간에는 보내지 않고 넘긴다. 보류했다 아침에 보내면 이미 지난 "1시간 전" 알림이 갈 수 있다.
  조용한 시간이 끝나면 그때 남은 시간으로 한 번 알린다.
- 사용자가 정한 알림이므로 하루 알림 상한에 세지 않는다 (`user_requested`).
"""

import logging
from datetime import datetime, timedelta

from app.core.config import NotificationSettings
from app.core.events import Event, EventKind, EventSource
from app.core.interfaces import GateAction
from app.scheduler.dispatcher import Dispatcher
from app.scheduler.gate import RuleBasedGate
from app.storage.todos import Todo, TodoRepository

logger = logging.getLogger(__name__)

DEFAULT_OFFSETS = NotificationSettings().deadline_reminders
# 확인 간격. 시점을 이만큼 일찍 알아챈다. 23:59 마감의 "1시간 전"(22:59)이 조용한 시간(23:00) 직전이라,
# 늦게 알아채면 조용한 시간에 걸려 사라진다. 일찍 알리는 편이 빠뜨리는 것보다 낫다.
CHECK_EVERY = timedelta(minutes=5)


def stage(todo: Todo, now: datetime, offsets: tuple[timedelta, ...]) -> timedelta | None:
    """지금 알릴 시점. 지난 시점 가운데 마감에 가장 가까운 것. 알릴 것이 없으면 None."""
    if todo.due_at is None or todo.due_at <= now:
        return None
    left = todo.due_at - now
    crossed = [offset for offset in offsets if left <= offset + CHECK_EVERY]
    if not crossed:
        return None
    nearest = min(crossed)
    # 할 일을 만들 때 이미 지나 있던 시점이다
    if todo.due_at - nearest < todo.created_at:
        return None
    return nearest


def remaining(left: timedelta) -> str:
    """남은 시간을 사람이 말하는 식으로. 멀면 거칠게, 가까우면 자세히."""
    minutes = int(left.total_seconds() // 60)
    days, hours = divmod(minutes // 60, 24)
    if days >= 3:
        return f"{days}일"
    if days >= 1:
        return f"{days}일 {hours}시간" if hours else f"{days}일"
    if minutes >= 60:
        rest = minutes % 60
        return f"{minutes // 60}시간 {rest}분" if rest >= 10 and minutes < 180 else f"{minutes // 60}시간"
    return f"{max(minutes, 1)}분"


def offset_key(offset: timedelta) -> str:
    hours = int(offset.total_seconds() // 3600)
    return f"{hours // 24}d" if hours % 24 == 0 else f"{hours}h"


class DeadlineReminder:
    def __init__(
        self,
        todos: TodoRepository,
        dispatcher: Dispatcher,
        gate: RuleBasedGate,
        offsets: tuple[timedelta, ...] = DEFAULT_OFFSETS,
    ) -> None:
        self._todos = todos
        self._dispatcher = dispatcher
        self._gate = gate
        self._offsets = tuple(sorted(set(offsets), reverse=True))

    async def run(self, now: datetime) -> int:
        """알린 건수를 돌려준다."""
        if not self._offsets or self._gate.in_quiet_hours(now):
            return 0
        sent = 0
        for todo in await self._todos.list_open():
            nearest = stage(todo, now, self._offsets)
            if nearest is None:
                continue
            decision = await self._dispatcher.publish(self._event(todo, nearest, now), now)
            if decision.action is GateAction.SEND_NOW:
                sent += 1
        if sent:
            logger.info("마감 리마인더 %d건", sent)
        return sent

    def _event(self, todo: Todo, nearest: timedelta, now: datetime) -> Event:
        assert todo.due_at is not None
        return Event(
            source=EventSource.SCHEDULER,
            kind=EventKind.REMINDER,
            title=f"{todo.title} 마감까지 {remaining(todo.due_at - now)} 남았습니다.",
            body="아직 끝내지 않은 할 일입니다.",
            urgent=True,
            user_requested=True,
            due_at=todo.due_at,
            ref_id=f"todo:{todo.id}:remind:{offset_key(nearest)}:{todo.due_at.isoformat()}",
            meta={"todo_id": todo.id},
        )

