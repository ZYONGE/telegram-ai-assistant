"""캘린더 도구: 조회, 빈 시간 찾기, 등록·수정·삭제.

- 계정 여러 개를 함께 본다. 조회는 연결된 계정을 모두 합치고, 쓰기는 한 계정을 골라서 한다.
- 등록·수정·삭제는 확인 버튼을 거친다 (CLAUDE.md 7절).
- 일정 식별자는 모델에 넘기지 않는다. 날짜와 제목으로 찾아 하나로 좁혀질 때만 실행한다.
"""

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from app.core.clock import format_kst, to_kst, utc_now
from app.core.interfaces import Confirmation, ToolResult
from app.google.accounts import GoogleAccount, GoogleAccounts
from app.google.auth import LOGIN_NEEDED, GoogleApiError, GoogleAuthError, TransientGoogleError
from app.google.calendar import CalendarEvent, day_range, free_slots, overlapping_pairs
from app.tools.common import SimpleTool, ToolInputError, optional_str, parse_datetime, spec

MAX_DAYS = 14
DEFAULT_MINUTES = 60
BUSY_MESSAGE = "캘린더가 잠시 응답하지 않습니다. 조금 뒤에 다시 시도해 주세요."
NOT_CONNECTED = LOGIN_NEEDED
_WEEKDAYS = "월화수목금토일"
# 빈 시간을 찾는 기본 시간대 (Asia/Seoul)
DAY_START, DAY_END = 9, 22


def relative_day(value: str | None, now: datetime):
    today = to_kst(now).date()
    text = (value or "오늘").strip().lower()
    if text in ("오늘", "today"):
        return today
    if text in ("내일", "tomorrow"):
        return today + timedelta(days=1)
    if text in ("모레",):
        return today + timedelta(days=2)
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        raise ToolInputError(
            f"날짜를 알아볼 수 없습니다: {value!r}. '오늘', '내일' 또는 2026-09-20 형식으로 적어 주세요."
        ) from None


def _int(args: Mapping[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = args.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ToolInputError(f"{key}는 {low}~{high} 사이 정수여야 합니다.")
    return value


def calendar_tools(accounts: GoogleAccounts, clock: Callable[[], datetime] = utc_now) -> list:
    def _account_property() -> dict[str, Any]:
        return {
            "account": {
                "type": "string",
                "enum": accounts.labels,
                "description": "계정 이름. 비우면 모든 계정(쓰기는 기본 계정)",
            }
        }

    async def _events(first, days: int, label: str | None = None):
        start, _ = day_range(first)
        _, end = day_range(first + timedelta(days=days - 1))
        return await accounts.events(start, end, label)

    async def _resolve(args: Mapping[str, Any], now: datetime) -> tuple[GoogleAccount, CalendarEvent]:
        """날짜와 제목으로 일정 하나를 찾는다. 여러 개면 사용자에게 되묻게 한다."""
        day = relative_day(optional_str(args, "day"), now)
        title = (optional_str(args, "title") or "").strip()
        if not title:
            raise ToolInputError("어떤 일정인지 title에 제목 일부를 적어 주세요.")
        result = await _events(day, 1, optional_str(args, "account"))
        matches = [event for event in result.events if title.lower() in event.title.lower()]
        if not matches:
            raise ToolInputError(f"{day.month}월 {day.day}일에 '{title}'과 맞는 일정이 없습니다. {result.warning}".strip())
        if len(matches) > 1:
            lines = "\n".join(event.render(with_account=accounts.multiple) for event in matches)
            raise ToolInputError(f"비슷한 일정이 여러 개입니다. 하나로 좁혀 주세요.\n{lines}")
        event = matches[0]
        account = accounts.find(event.account) or accounts.default_account()
        if account is None:
            raise ToolInputError(NOT_CONNECTED)
        return account, event

    async def list_events(args: Mapping[str, Any]) -> ToolResult:
        now = clock()
        first = relative_day(optional_str(args, "start"), now)
        days = _int(args, "days", 1, 1, MAX_DAYS)
        result = await _events(first, days, optional_str(args, "account"))
        if not result.events:
            span = "그날" if days == 1 else "그 기간에"
            return ToolResult(f"{span} 등록된 일정이 없습니다. {result.warning}".strip())
        lines = [event.render(with_date=days > 1, with_account=accounts.multiple) for event in result.events]
        if result.warning:
            lines.append(result.warning)
        return ToolResult("\n".join(lines))

    async def find_free_time(args: Mapping[str, Any]) -> ToolResult:
        now = clock()
        day = relative_day(optional_str(args, "day"), now)
        minutes = _int(args, "minutes", DEFAULT_MINUTES, 15, 480)
        result = await _events(day, 1)
        slots = free_slots(result.events, day, minutes, DAY_START, DAY_END)
        if not slots:
            return ToolResult(f"{day.month}월 {day.day}일에는 {minutes}분 이상 비는 시간이 없습니다.")
        label = f"{day.month}월 {day.day}일({_WEEKDAYS[day.weekday()]}) 비는 시간"
        lines = [f"{to_kst(begin):%H:%M}~{to_kst(finish):%H:%M}" for begin, finish in slots]
        return ToolResult("\n".join([label, *lines, result.warning]).strip())

    return [
        SimpleTool(
            spec(
                "list_events",
                "캘린더 일정을 조회한다. 연결된 계정을 모두 합쳐서 보여 준다. "
                "일정·약속·수업을 묻거나, 할 일을 언제 할지 정할 때 먼저 확인한다.",
                {
                    "start": {"type": "string", "description": "시작 날짜. '오늘', '내일' 또는 2026-09-20 (기본값 오늘)"},
                    "days": {"type": "integer", "description": f"조회할 일수 1~{MAX_DAYS} (기본값 1)"},
                    **_account_property(),
                },
                [],
            ),
            list_events,
        ),
        SimpleTool(
            spec(
                "find_free_time",
                f"하루 중 일정이 없는 시간대를 찾는다 ({DAY_START}시~{DAY_END}시, 모든 계정 기준). "
                "'언제 시간 비어?', '어디에 넣을까?'라고 물을 때 쓴다.",
                {
                    "day": {"type": "string", "description": "찾을 날. '오늘', '내일' 또는 2026-09-20 (기본값 오늘)"},
                    "minutes": {"type": "integer", "description": "필요한 시간(분), 15~480 (기본값 60)"},
                },
                [],
            ),
            find_free_time,
        ),
        AddEventTool(accounts, clock, _account_property()),
        UpdateEventTool(accounts, clock, _resolve, _account_property()),
        DeleteEventTool(accounts, clock, _resolve, _account_property()),
    ]


class AddEventTool:
    """일정 등록. 확인 버튼을 받은 뒤 실행하고, 겹치는 일정이 있으면 확인 문구에 알린다."""

    def __init__(self, accounts: GoogleAccounts, clock, account_property: dict) -> None:
        self._accounts = accounts
        self._clock = clock
        self.spec = spec(
            "add_event",
            "캘린더에 일정을 등록한다. 실행 전에 확인 버튼이 전송되고, 사용자가 확인해야 등록된다. "
            "마감만 있는 일은 일정 대신 add_todo를 쓴다.",
            {
                "title": {"type": "string", "description": "일정 제목"},
                "start": {"type": "string", "description": "시작 시각 (2026-09-20T18:00). 시간대가 없으면 Asia/Seoul"},
                "end": {"type": "string", "description": "끝 시각 (선택). 없으면 minutes만큼 잡는다"},
                "minutes": {"type": "integer", "description": f"길이(분). 기본값 {DEFAULT_MINUTES}"},
                "location": {"type": "string", "description": "장소 (선택)"},
                **account_property,
            },
            ["title", "start"],
            confirmation=Confirmation.BUTTON,
        )

    async def describe(self, args: Mapping[str, Any]) -> str:
        account, title, start, end, location = await self._plan(args)
        where = f" ({location})" if location else ""
        line = f"일정 등록 — [{account.label}] {format_kst(start)}~{to_kst(end):%H:%M} {title}{where}"
        clash = await self._conflicts(start, end)
        return f"{line}\n겹치는 일정: {clash}" if clash else line

    async def run(self, args: Mapping[str, Any]) -> ToolResult:
        account, title, start, end, location = await self._plan(args)
        try:
            event = await account.calendar.create_event(title, start, end, location=location)
        except TransientGoogleError:
            return ToolResult(BUSY_MESSAGE, is_error=True)
        except (GoogleApiError, GoogleAuthError) as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult(f"등록했습니다: [{account.label}] {event.render(with_date=True)}")

    async def _plan(self, args: Mapping[str, Any]):
        account = self._accounts.find(optional_str(args, "account")) or self._accounts.default_account()
        if account is None or not account.connected:
            raise ToolInputError(NOT_CONNECTED)
        title = (optional_str(args, "title") or "").strip()
        if not title:
            raise ToolInputError("일정 제목이 필요합니다.")
        start = parse_datetime(optional_str(args, "start") or "")
        minutes = _int(args, "minutes", DEFAULT_MINUTES, 5, 1440)
        end_text = optional_str(args, "end")
        end = parse_datetime(end_text) if end_text else start + timedelta(minutes=minutes)
        if end <= start:
            raise ToolInputError("끝 시각이 시작보다 빠릅니다.")
        return account, title, start, end, optional_str(args, "location") or ""

    async def _conflicts(self, start: datetime, end: datetime) -> str:
        result = await self._accounts.events(start, end)
        candidate = CalendarEvent(id="", title="", start=start, end=end)
        clashing = [event for event in result.events if not event.all_day and event.overlaps(candidate)]
        return ", ".join(event.render(with_account=self._accounts.multiple) for event in clashing)


class UpdateEventTool:
    """일정 수정. 날짜와 제목으로 하나를 찾아 확인 버튼 뒤에 고친다."""

    def __init__(self, accounts: GoogleAccounts, clock, resolve, account_property: dict) -> None:
        self._accounts = accounts
        self._clock = clock
        self._resolve = resolve
        self.spec = spec(
            "update_event",
            "캘린더 일정의 제목·시각·장소를 수정한다. 날짜와 제목 일부로 일정을 찾는다. "
            "실행 전에 확인 버튼이 전송된다.",
            {
                "day": {"type": "string", "description": "일정이 있는 날. '오늘', '내일' 또는 2026-09-20"},
                "title": {"type": "string", "description": "찾을 제목 일부"},
                "new_title": {"type": "string", "description": "새 제목 (선택)"},
                "new_start": {"type": "string", "description": "새 시작 시각 (선택)"},
                "new_end": {"type": "string", "description": "새 끝 시각 (선택)"},
                "new_location": {"type": "string", "description": "새 장소 (선택)"},
                **account_property,
            },
            ["day", "title"],
            confirmation=Confirmation.BUTTON,
        )

    async def describe(self, args: Mapping[str, Any]) -> str:
        account, event = await self._resolve(args, self._clock())
        changes = self._changes(args)
        if not changes:
            raise ToolInputError("바꿀 내용(new_title, new_start, new_end, new_location) 중 하나는 있어야 합니다.")
        parts = []
        if "title" in changes:
            parts.append(f"제목 → {changes['title']}")
        if "start" in changes:
            parts.append(f"시작 → {format_kst(changes['start'])}")
        if "end" in changes:
            parts.append(f"끝 → {to_kst(changes['end']):%H:%M}")
        if "location" in changes:
            parts.append(f"장소 → {changes['location'] or '(지움)'}")
        return f"일정 수정 — [{account.label}] {event.render()}\n{', '.join(parts)}"

    async def run(self, args: Mapping[str, Any]) -> ToolResult:
        account, event = await self._resolve(args, self._clock())
        changes = self._changes(args)
        try:
            updated = await account.calendar.update_event(event.id, **changes)
        except TransientGoogleError:
            return ToolResult(BUSY_MESSAGE, is_error=True)
        except (GoogleApiError, GoogleAuthError) as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult(f"수정했습니다: [{account.label}] {updated.render(with_date=True)}")

    def _changes(self, args: Mapping[str, Any]) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        if (title := optional_str(args, "new_title")):
            changes["title"] = title
        if (start := optional_str(args, "new_start")):
            changes["start"] = parse_datetime(start)
        if (end := optional_str(args, "new_end")):
            changes["end"] = parse_datetime(end)
        if (location := optional_str(args, "new_location")) is not None:
            changes["location"] = location
        return changes


class DeleteEventTool:
    """일정 삭제. 확인 버튼을 받은 뒤 지운다."""

    def __init__(self, accounts: GoogleAccounts, clock, resolve, account_property: dict) -> None:
        self._accounts = accounts
        self._clock = clock
        self._resolve = resolve
        self.spec = spec(
            "delete_event",
            "캘린더 일정을 삭제한다. 날짜와 제목 일부로 일정을 찾는다. 실행 전에 확인 버튼이 전송된다.",
            {
                "day": {"type": "string", "description": "일정이 있는 날. '오늘', '내일' 또는 2026-09-20"},
                "title": {"type": "string", "description": "찾을 제목 일부"},
                **account_property,
            },
            ["day", "title"],
            confirmation=Confirmation.BUTTON,
        )

    async def describe(self, args: Mapping[str, Any]) -> str:
        account, event = await self._resolve(args, self._clock())
        return f"일정 삭제 — [{account.label}] {event.render(with_date=True)}"

    async def run(self, args: Mapping[str, Any]) -> ToolResult:
        account, event = await self._resolve(args, self._clock())
        try:
            await account.calendar.delete_event(event.id)
        except TransientGoogleError:
            return ToolResult(BUSY_MESSAGE, is_error=True)
        except (GoogleApiError, GoogleAuthError) as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult(f"삭제했습니다: [{account.label}] {event.render(with_date=True)}")


def not_connected_tools() -> list:
    """Google 연결 전에도 도구 이름은 그대로 두고, 연결이 필요하다고 답한다."""

    async def unavailable(args: Mapping[str, Any]) -> ToolResult:
        return ToolResult(NOT_CONNECTED, is_error=True)

    names = {
        "list_events": "캘린더 일정을 조회한다.",
        "find_free_time": "일정이 없는 시간대를 찾는다.",
        "add_event": "캘린더에 일정을 등록한다.",
        "update_event": "캘린더 일정을 수정한다.",
        "delete_event": "캘린더 일정을 삭제한다.",
    }
    return [
        SimpleTool(spec(name, f"{description} 아직 Google 계정이 연결되지 않았다.", {}, []), unavailable)
        for name, description in names.items()
    ]
