"""Google 캘린더 조회.

일정은 브리핑과 대화 도구가 함께 쓴다. 등록·수정·삭제(확인 버튼)는 다음 모듈에서 붙인다.
응답에서 필요한 항목만 뽑아 쓰고, 원문이나 토큰은 오류 메시지에 담지 않는다.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import httpx

from app.core.clock import KST, to_kst
from app.google.auth import GoogleApiError, GoogleAuth, GoogleAuthError, TransientGoogleError

logger = logging.getLogger(__name__)

CALENDAR_API = "https://www.googleapis.com/calendar/v3"
MAX_EVENTS = 25
_WEEKDAYS = "월화수목금토일"


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str = ""
    # 어느 계정의 일정인지 (여러 계정을 함께 볼 때 표시)
    account: str = ""

    def render(self, with_date: bool = False, with_account: bool = False) -> str:
        local_start, local_end = to_kst(self.start), to_kst(self.end)
        who = f"[{self.account}] " if with_account and self.account else ""
        day = f"{local_start.month}월 {local_start.day}일({_WEEKDAYS[local_start.weekday()]}) " if with_date else ""
        when = "종일" if self.all_day else f"{local_start:%H:%M}~{local_end:%H:%M}"
        where = f" ({self.location})" if self.location else ""
        return f"{who}{day}{when} {self.title}{where}"

    def overlaps(self, other: "CalendarEvent") -> bool:
        return self.start < other.end and other.start < self.end


def parse_event(item: dict) -> CalendarEvent | None:
    """캘린더 항목 하나를 우리 형식으로. 취소된 일정이나 시각이 없는 항목은 건너뛴다."""
    if item.get("status") == "cancelled":
        return None
    start, end = item.get("start", {}), item.get("end", {})
    try:
        if "dateTime" in start:
            begins, finishes, all_day = _moment(start["dateTime"]), _moment(end.get("dateTime")), False
        elif "date" in start:
            first = date.fromisoformat(start["date"])
            # 종일 일정의 종료일은 다음 날이라 하루를 빼서 표시 범위를 맞춘다
            last = date.fromisoformat(end.get("date", start["date"]))
            begins = datetime.combine(first, time(0), tzinfo=KST)
            finishes = datetime.combine(max(last - timedelta(days=1), first), time(23, 59), tzinfo=KST)
            all_day = True
        else:
            return None
    except ValueError:
        logger.info("캘린더 항목의 시각을 읽지 못해 건너뜁니다")
        return None
    return CalendarEvent(
        id=str(item.get("id", "")),
        title=(item.get("summary") or "(제목 없음)").strip(),
        start=begins,
        end=finishes or begins,
        all_day=all_day,
        location=(item.get("location") or "").strip(),
    )


def _moment(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def day_range(day: date) -> tuple[datetime, datetime]:
    """Asia/Seoul 기준 하루의 시작과 끝."""
    start = datetime.combine(day, time(0), tzinfo=KST)
    return start, start + timedelta(days=1)


def overlapping_pairs(events: list[CalendarEvent]) -> list[tuple[CalendarEvent, CalendarEvent]]:
    """시간이 겹치는 일정 쌍. 종일 일정은 겹침으로 보지 않는다."""
    timed = [event for event in events if not event.all_day]
    ordered = sorted(timed, key=lambda event: event.start)
    return [
        (first, second)
        for index, first in enumerate(ordered)
        for second in ordered[index + 1 :]
        if first.overlaps(second)
    ]


def free_slots(
    events: list[CalendarEvent], day: date, minutes: int, day_start: int = 9, day_end: int = 22
) -> list[tuple[datetime, datetime]]:
    """하루 중 일정이 없는 구간. 종일 일정은 하루 전체를 채운 것으로 본다."""
    window_start = datetime.combine(day, time(day_start), tzinfo=KST)
    window_end = datetime.combine(day, time(0) if day_end == 24 else time(day_end), tzinfo=KST)
    if day_end == 24:
        window_end += timedelta(days=1)
    need = timedelta(minutes=minutes)

    busy: list[tuple[datetime, datetime]] = []
    for event in events:
        begin, finish = max(to_kst(event.start), window_start), min(to_kst(event.end), window_end)
        if finish > begin:
            busy.append((begin, finish))
    busy.sort()

    slots: list[tuple[datetime, datetime]] = []
    cursor = window_start
    for begin, finish in busy:
        if begin - cursor >= need:
            slots.append((cursor, begin))
        cursor = max(cursor, finish)
    if window_end - cursor >= need:
        slots.append((cursor, window_end))
    return slots


class CalendarClient:
    def __init__(self, auth: GoogleAuth, http: httpx.AsyncClient, calendar_id: str = "primary") -> None:
        self._auth = auth
        self._http = http
        self._calendar_id = calendar_id

    @property
    def configured(self) -> bool:
        return self._auth.configured

    async def list_events(self, start: datetime, end: datetime, limit: int = MAX_EVENTS) -> list[CalendarEvent]:
        """시작 시각 순으로 정렬된 일정. 반복 일정은 펼쳐서 받는다."""
        payload = await self._get(
            f"/calendars/{self._calendar_id}/events",
            {
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": str(limit),
                "timeZone": "Asia/Seoul",
            },
        )
        events = [parsed for item in payload.get("items", []) if (parsed := parse_event(item))]
        return sorted(events, key=lambda event: event.start)

    async def events_on(self, day: date, limit: int = MAX_EVENTS) -> list[CalendarEvent]:
        start, end = day_range(day)
        return await self.list_events(start, end, limit)

    async def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        *,
        location: str = "",
        description: str = "",
        all_day: bool = False,
    ) -> CalendarEvent:
        body = _body(title, start, end, location=location, description=description, all_day=all_day)
        payload = await self._request("POST", f"/calendars/{self._calendar_id}/events", json=body)
        created = parse_event(payload)
        if created is None:
            raise GoogleApiError("일정을 만들었지만 결과를 읽지 못했습니다.")
        return created

    async def update_event(
        self,
        event_id: str,
        *,
        title: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        location: str | None = None,
    ) -> CalendarEvent:
        body: dict = {}
        if title is not None:
            body["summary"] = title
        if location is not None:
            body["location"] = location
        if start is not None:
            body["start"] = {"dateTime": start.isoformat(), "timeZone": "Asia/Seoul"}
        if end is not None:
            body["end"] = {"dateTime": end.isoformat(), "timeZone": "Asia/Seoul"}
        if not body:
            raise GoogleApiError("바꿀 내용이 없습니다.")
        payload = await self._request("PATCH", f"/calendars/{self._calendar_id}/events/{event_id}", json=body)
        updated = parse_event(payload)
        if updated is None:
            raise GoogleApiError("일정을 수정했지만 결과를 읽지 못했습니다.")
        return updated

    async def delete_event(self, event_id: str) -> None:
        await self._request("DELETE", f"/calendars/{self._calendar_id}/events/{event_id}")

    async def get_event(self, event_id: str) -> CalendarEvent | None:
        payload = await self._request("GET", f"/calendars/{self._calendar_id}/events/{event_id}")
        return parse_event(payload)

    async def _get(self, path: str, params: dict[str, str]) -> dict:
        return await self._request("GET", path, params=params)

    async def _request(self, method: str, path: str, params: dict | None = None, json: dict | None = None) -> dict:
        token = await self._auth.access_token()
        try:
            response = await self._http.request(
                method,
                CALENDAR_API + path,
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise TransientGoogleError(f"캘린더 연결 실패 ({type(exc).__name__})") from None
        if response.status_code in (200, 201):
            try:
                return response.json()
            except ValueError:
                raise GoogleApiError("캘린더 응답을 읽지 못했습니다.") from None
        if response.status_code in (204, 410):
            # 삭제 성공, 또는 이미 지워진 일정
            return {}
        if response.status_code == 401:
            raise GoogleAuthError("Google 연결이 만료되었습니다. python -m app.google.login으로 다시 연결해 주세요.")
        if response.status_code == 404:
            raise GoogleApiError("캘린더나 일정을 찾지 못했습니다.")
        if response.status_code in (403, 429) or response.status_code >= 500:
            raise TransientGoogleError(f"캘린더 요청이 잠시 막혔습니다 (HTTP {response.status_code}).")
        raise GoogleApiError(f"캘린더 요청 오류 (HTTP {response.status_code}).")


def _body(
    title: str,
    start: datetime,
    end: datetime,
    *,
    location: str = "",
    description: str = "",
    all_day: bool = False,
) -> dict:
    if all_day:
        # 종일 일정의 종료일은 다음 날로 넣어야 한다
        when = {
            "start": {"date": to_kst(start).date().isoformat()},
            "end": {"date": (to_kst(end).date() + timedelta(days=1)).isoformat()},
        }
    else:
        when = {
            "start": {"dateTime": start.isoformat(), "timeZone": "Asia/Seoul"},
            "end": {"dateTime": end.isoformat(), "timeZone": "Asia/Seoul"},
        }
    body = {"summary": title, **when}
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    return body
