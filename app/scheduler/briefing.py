"""아침·저녁 브리핑.

각 기능은 BriefingProvider로 항목만 제공하고, 조립과 발송은 여기서 한다 (CLAUDE.md 4-1).
초안은 코드로 만들고, 항목이 있을 때만 가벼운 모델로 문장을 다듬는다. 다듬기에 실패하면 초안을 보낸다.
"""

import logging
from datetime import date, datetime, time, timedelta
from typing import Protocol

from app.agent.prompt import DEFAULT_HONORIFIC
from app.core.clock import KST, format_kst, to_kst
from app.core.events import Event, EventKind, EventSource
from app.core.interfaces import BriefingItem, BriefingKind, BriefingProvider, GateAction, GateDecision
from app.scheduler.dispatcher import Dispatcher
from app.scheduler.tasks import TaskService
from app.storage.notifications import NotificationLog
from app.storage.todos import Todo, TodoRepository

logger = logging.getLogger(__name__)

_WEEKDAYS = "월화수목금토일"
TITLES = {BriefingKind.MORNING: "아침 브리핑", BriefingKind.EVENING: "저녁 브리핑"}


class Polisher(Protocol):
    async def polish_briefing(self, kind: BriefingKind, draft: str) -> str: ...


def _day(value: datetime) -> date:
    return to_kst(value).date()


def _todo_line(todo: Todo) -> str:
    return f"{todo.title} (마감 {format_kst(todo.due_at)})" if todo.due_at else todo.title


class TodoBriefing:
    name = "todos"

    def __init__(self, repo: TodoRepository) -> None:
        self._repo = repo

    async def briefing_items(self, kind: BriefingKind, now: datetime) -> list[BriefingItem]:
        today = _day(now)
        todos = [t for t in await self._repo.list_open() if t.due_at is not None]
        if kind is BriefingKind.MORNING:
            items = [BriefingItem("오늘 마감", _todo_line(t), 30) for t in todos if _day(t.due_at) == today]
            overdue = [t for t in todos if _day(t.due_at) < today]
            if overdue:
                titles = ", ".join(t.title for t in overdue[:5])
                items.append(BriefingItem("마감이 지난 할 일", f"{len(overdue)}건: {titles}", 20))
            items += [
                BriefingItem("다가오는 마감 (3일 이내)", _todo_line(t), 10)
                for t in todos
                if today < _day(t.due_at) <= today + timedelta(days=3)
            ]
            return items

        tomorrow = today + timedelta(days=1)
        items = [BriefingItem("내일 마감", _todo_line(t), 30) for t in todos if _day(t.due_at) == tomorrow]
        items += [BriefingItem("오늘 마감인데 남은 일", _todo_line(t), 25) for t in todos if _day(t.due_at) == today]
        start = datetime.combine(today, time(0), tzinfo=KST)
        done = await self._repo.list_done_between(start, now)
        if done:
            items.append(BriefingItem("오늘 한 일", f"할 일 {len(done)}건 완료", 5))
        return items


class TaskBriefing:
    name = "tasks"

    def __init__(self, service: TaskService) -> None:
        self._service = service

    async def briefing_items(self, kind: BriefingKind, now: datetime) -> list[BriefingItem]:
        target = _day(now) + timedelta(days=0 if kind is BriefingKind.MORNING else 1)
        section = "오늘 리마인더" if kind is BriefingKind.MORNING else "내일 리마인더"
        items = []
        for task in await self._service.list():
            next_run = self._service.next_run(task.id)
            if task.status == "active" and next_run and _day(next_run) == target:
                items.append(BriefingItem(section, f"{to_kst(next_run):%H:%M} {task.content}", 15))
        return items


class NewsBriefing:
    """게이트가 브리핑으로 미룬 소식. 브리핑을 보낸 뒤 acknowledge()로 표시해 다음에는 빼낸다."""

    name = "news"

    def __init__(self, log: NotificationLog) -> None:
        self._log = log
        self._shown: list[str] = []

    async def briefing_items(self, kind: BriefingKind, now: datetime) -> list[BriefingItem]:
        records = await self._log.unbriefed_batch()
        self._shown = [r.event.ref_id for r in records]
        return [
            BriefingItem(
                "새 소식",
                r.event.title + (f" (마감 {format_kst(r.event.due_at)})" if r.event.due_at else ""),
                12,
            )
            for r in records
        ]

    async def acknowledge(self, now: datetime) -> None:
        await self._log.mark_briefed(self._shown, now)
        self._shown = []


def compose(kind: BriefingKind, items: list[BriefingItem], now: datetime, honorific: str = DEFAULT_HONORIFIC) -> str:
    local = to_kst(now)
    today = f"{local.month}월 {local.day}일({_WEEKDAYS[local.weekday()]})"
    if kind is BriefingKind.MORNING:
        lines = [f"{honorific}, 좋은 아침입니다. {today} 브리핑입니다."]
        empty = "오늘 따로 챙길 마감이나 소식은 없습니다."
    else:
        lines = [f"{honorific}, 오늘 정리와 내일 준비 사항입니다."]
        empty = "내일 따로 챙길 일은 없습니다."
    if not items:
        return "\n".join([*lines, empty])

    sections: dict[str, list[str]] = {}
    for item in sorted(items, key=lambda i: -i.priority):
        sections.setdefault(item.section, []).append(item.text)
    for section, texts in sections.items():
        lines += ["", section, *(f"· {text}" for text in texts)]
    return "\n".join(lines)


class BriefingService:
    def __init__(
        self,
        providers: list[BriefingProvider],
        dispatcher: Dispatcher,
        polisher: Polisher | None,
        honorific: str = DEFAULT_HONORIFIC,
    ) -> None:
        self._providers = providers
        self._dispatcher = dispatcher
        self._polisher = polisher
        self._honorific = honorific

    async def send(self, kind: BriefingKind, now: datetime) -> GateDecision:
        items: list[BriefingItem] = []
        for provider in self._providers:
            try:
                items += await provider.briefing_items(kind, now)
            except Exception:
                logger.exception("브리핑 항목 수집 실패: %s", provider.name)

        text = compose(kind, items, now, self._honorific)
        if items and self._polisher is not None:
            text = await self._polisher.polish_briefing(kind, text)

        event = Event(
            source=EventSource.SCHEDULER,
            kind=EventKind.BRIEFING,
            title=TITLES[kind],
            body=text,
            ref_id=f"briefing:{kind}:{to_kst(now):%Y%m%d}",
        )
        decision = await self._dispatcher.publish(event, now)
        if decision.action is GateAction.SEND_NOW:
            for provider in self._providers:
                acknowledge = getattr(provider, "acknowledge", None)
                if acknowledge is not None:
                    await acknowledge(now)
        return decision
