"""아침·저녁 브리핑.

각 기능은 BriefingProvider로 항목만 제공하고, 조립과 발송은 여기서 한다 (CLAUDE.md 4-1).
초안은 코드로 만들고, 항목이 있을 때만 가벼운 모델로 문장을 다듬는다. 다듬기에 실패하면 초안을 보낸다.
"""

import logging
from datetime import date, datetime, time, timedelta
from typing import Protocol

from app.agent.prompt import DEFAULT_HONORIFIC
from app.collectors.weather import KmaWeather, WeatherUnavailable
from app.google.accounts import GoogleAccounts
from app.google.calendar import overlapping_pairs
from app.core.clock import KST, format_kst, to_kst
from app.core.events import Event, EventKind, EventSource
from app.core.interfaces import BriefingItem, BriefingKind, BriefingProvider, GateAction, GateDecision
from app.mail.service import MailService, undo_data
from app.scheduler.dispatcher import Dispatcher
from app.scheduler.tasks import TaskService
from app.storage.notifications import NotificationLog
from app.storage.todos import Todo, TodoRepository

logger = logging.getLogger(__name__)

_WEEKDAYS = "월화수목금토일"
TITLES = {
    BriefingKind.MORNING: "아침 브리핑",
    BriefingKind.EVENING: "저녁 브리핑",
    BriefingKind.WEEKLY: "주간 계획",
}
# 주간 계획은 일요일 저녁에 다음 주(월~일)를 본다
WEEK_DAYS = 7


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
        opened = await self._repo.list_open()
        todos = [t for t in opened if t.due_at is not None]
        if kind is BriefingKind.WEEKLY:
            start, end = today + timedelta(days=1), today + timedelta(days=WEEK_DAYS)
            items = [
                BriefingItem("다음 주 마감", _todo_line(t), 30) for t in todos if start <= _day(t.due_at) <= end
            ]
            items += [BriefingItem("아직 남은 일", _todo_line(t), 20) for t in todos if _day(t.due_at) <= today]
            items += [BriefingItem("마감 없는 할 일", t.title, 10) for t in opened if t.due_at is None][:5]
            return items
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
        today = _day(now)
        if kind is BriefingKind.WEEKLY:
            window = (today + timedelta(days=1), today + timedelta(days=WEEK_DAYS))
            section = "다음 주 리마인더"
        else:
            target = today + timedelta(days=0 if kind is BriefingKind.MORNING else 1)
            window = (target, target)
            section = "오늘 리마인더" if kind is BriefingKind.MORNING else "내일 리마인더"

        items = []
        for task in await self._service.list():
            next_run = self._service.next_run(task.id)
            if task.status != "active" or next_run is None or not window[0] <= _day(next_run) <= window[1]:
                continue
            when = f"{to_kst(next_run):%H:%M}" if section != "다음 주 리마인더" else format_kst(next_run)
            items.append(BriefingItem(section, f"{when} {task.content}", 15))
        return items


class CalendarBriefing:
    """아침에는 오늘 일정, 저녁에는 내일 일정, 주간 계획에는 다음 주 일정. 겹치는 일정은 따로 알린다."""

    name = "calendar"

    def __init__(self, accounts: GoogleAccounts) -> None:
        self._accounts = accounts

    async def briefing_items(self, kind: BriefingKind, now: datetime) -> list[BriefingItem]:
        if not self._accounts.ready:
            return []
        today = _day(now)
        if kind is BriefingKind.MORNING:
            first, days, section, priority = today, 1, "오늘 일정", 38
        elif kind is BriefingKind.EVENING:
            first, days, section, priority = today + timedelta(days=1), 1, "내일 일정", 28
        else:
            first, days, section, priority = today + timedelta(days=1), WEEK_DAYS, "다음 주 일정", 26

        start = datetime.combine(first, time(0), tzinfo=KST)
        result = await self._accounts.events(start, start + timedelta(days=days))
        show_account = self._accounts.multiple
        items = [
            BriefingItem(section, event.render(with_date=days > 1, with_account=show_account), priority)
            for event in result.events
        ]
        for first_event, second_event in overlapping_pairs(result.events):
            items.append(
                BriefingItem(
                    "일정 겹침",
                    f"{first_event.render(with_account=show_account)} ↔ {second_event.render(with_account=show_account)}",
                    45,
                )
            )
        if result.failed:
            items.append(BriefingItem("확인하지 못한 것", f"{', '.join(result.failed)} 계정 일정", 1))
        return items


class WeatherBriefing:
    """아침에는 오늘 날씨와 옷차림, 저녁에는 내일 아침 준비용 한 줄.

    옷차림 기본안은 수집기(코드)가 만들고, 모델은 브리핑을 다듬을 때 문장만 손본다.
    """

    name = "weather"

    def __init__(self, weather: KmaWeather) -> None:
        self._weather = weather

    async def briefing_items(self, kind: BriefingKind, now: datetime) -> list[BriefingItem]:
        if kind is BriefingKind.WEEKLY or not self._weather.enabled:
            return []
        morning = kind is BriefingKind.MORNING
        try:
            forecast = await self._weather.forecast(now, 0 if morning else 1)
        except WeatherUnavailable as exc:
            logger.info("날씨 항목 생략: %s", exc)
            return []
        if morning:
            return [
                BriefingItem("오늘 날씨", forecast.render(), 40),
                BriefingItem("오늘 날씨", forecast.clothing(), 39),
            ]
        return [BriefingItem("내일 날씨", forecast.render(), 9)]


class MailBriefing:
    """아침에는 그 외 메일 목록, 저녁에는 정리 내역과 답변 대기 상기.

    유형별 즉시 알림은 수집기가 이벤트로 보내고, 여기서는 묶어서 보여 줄 것만 맡는다.
    """

    name = "mail"

    def __init__(self, service: MailService) -> None:
        self._service = service
        self._listed = False
        self._reminded: list = []

    async def briefing_items(self, kind: BriefingKind, now: datetime) -> list[BriefingItem]:
        if kind is BriefingKind.WEEKLY:
            return []
        items: list[BriefingItem] = []
        if kind is BriefingKind.MORNING:
            pending = await self._service.morning_list()
            self._listed = bool(pending)
            items += [
                BriefingItem("새 메일", f"[{account}] {subject}" if account else subject, 14)
                for account, subject in pending
            ]
        else:
            cleaned = await self._service.cleaned_today(now)
            for place, records in _by_place(cleaned).items():
                titles = ", ".join(record.subject[:20] for record in records[:3])
                items.append(BriefingItem("메일 정리", f"{len(records)}건을 {place}으로 옮겼습니다: {titles}", 6))

        overdue = await self._service.overdue_waiting(now)
        self._reminded = overdue
        items += [
            BriefingItem("답장이 아직 안 나간 메일", f"{item.sender} — {item.subject[:40]}", 18) for item in overdue
        ]
        return items

    async def briefing_buttons(self, kind: BriefingKind, now: datetime) -> list[dict]:
        if kind is BriefingKind.EVENING and await self._service.cleaned_today(now):
            return [{"label": "메일 정리 되돌리기", "data": undo_data(now)}]
        return []

    async def acknowledge(self, now: datetime) -> None:
        if self._listed:
            await self._service.mark_morning_listed()
            self._listed = False
        if self._reminded:
            await self._service.mark_reminded(self._reminded, now)
            self._reminded = []


PLACES = {"trash": "휴지통", "spam": "스팸함", "file": "영수증 보관함"}


def _by_place(records: list) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for record in records:
        grouped.setdefault(PLACES.get(record.action, "휴지통"), []).append(record)
    return grouped


class NewsBriefing:
    """게이트가 브리핑으로 미룬 소식. 브리핑을 보낸 뒤 acknowledge()로 표시해 다음에는 빼낸다."""

    name = "news"

    def __init__(self, log: NotificationLog) -> None:
        self._log = log
        self._shown: list[str] = []

    async def briefing_items(self, kind: BriefingKind, now: datetime) -> list[BriefingItem]:
        if kind is BriefingKind.WEEKLY:
            return []
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
    if kind is BriefingKind.WEEKLY:
        lines = [f"{honorific}, 다음 주 계획입니다. (오늘은 {today})"]
        empty = "다음 주에 챙길 마감이나 예약은 없습니다."
    elif kind is BriefingKind.MORNING:
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

        buttons: list[dict] = []
        for provider in self._providers:
            make_buttons = getattr(provider, "briefing_buttons", None)
            if make_buttons is not None:
                try:
                    buttons += await make_buttons(kind, now)
                except Exception:
                    logger.exception("브리핑 버튼 수집 실패: %s", provider.name)

        event = Event(
            source=EventSource.SCHEDULER,
            kind=EventKind.BRIEFING,
            title=TITLES[kind],
            body=text,
            ref_id=f"briefing:{kind}:{to_kst(now):%Y%m%d}",
            meta={"buttons": buttons} if buttons else {},
        )
        decision = await self._dispatcher.publish(event, now)
        if decision.action is GateAction.SEND_NOW:
            for provider in self._providers:
                acknowledge = getattr(provider, "acknowledge", None)
                if acknowledge is not None:
                    await acknowledge(now)
        return decision
