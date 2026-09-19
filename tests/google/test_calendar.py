import json
from dataclasses import replace

import httpx
import pytest

from app.core.config import GoogleAccountSettings, GoogleSettings
from app.google.accounts import GoogleAccounts
from app.google.auth import GoogleApiError, GoogleAuthError, StoredToken, TransientGoogleError
from app.google.calendar import (
    CalendarClient,
    free_slots,
    overlapping_pairs,
    parse_event,
)
from tests.conftest import kst
from tests.google.test_auth import CLIENT_JSON

TIMED = {
    "id": "evt-1",
    "summary": "자료구조",
    "location": "공학관 301",
    "start": {"dateTime": "2026-09-18T09:00:00+09:00"},
    "end": {"dateTime": "2026-09-18T10:30:00+09:00"},
}
ALL_DAY = {
    "id": "evt-2",
    "summary": "개교기념일",
    "start": {"date": "2026-09-18"},
    "end": {"date": "2026-09-19"},
}


def events_payload(*items) -> dict:
    return {"items": list(items)}


class FakeAuth:
    """토큰 발급만 흉내 낸다. 갱신 동작은 test_auth.py에서 따로 본다."""

    def __init__(self, token: str = "token-1", configured: bool = True) -> None:
        self._token = token
        self.configured = configured

    async def access_token(self) -> str:
        return self._token


def client_with(handler, calendar_id: str = "primary"):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CalendarClient(FakeAuth(), http, calendar_id), http


# --- 항목 읽기 ---


def test_parse_timed_and_all_day_events():
    timed = parse_event(TIMED)
    assert timed.title == "자료구조" and timed.all_day is False
    assert timed.start == kst(9, 18, 9) and timed.end == kst(9, 18, 10, 30)
    assert timed.render() == "09:00~10:30 자료구조 (공학관 301)"

    whole = parse_event(ALL_DAY)
    # 종일 일정의 종료일은 다음 날로 오므로 하루를 빼서 그날 안에 둔다
    assert whole.all_day is True and whole.start.date() == whole.end.date()
    assert whole.render() == "종일 개교기념일"


@pytest.mark.parametrize(
    "item",
    [
        {"id": "x", "status": "cancelled", "start": {"dateTime": "2026-09-18T09:00:00+09:00"}},
        {"id": "y", "summary": "시각 없음"},
        {"id": "z", "start": {"dateTime": "언제였더라"}, "end": {}},
    ],
)
def test_unusable_items_are_skipped(item):
    assert parse_event(item) is None


def test_render_options():
    event = parse_event(TIMED)
    assert event.render(with_date=True).startswith("9월 18일(금) ")
    tagged = replace(event, account="학교")
    assert tagged.render(with_account=True) == "[학교] 09:00~10:30 자료구조 (공학관 301)"
    # 계정 이름은 함께 볼 때만 붙인다
    assert tagged.render() == event.render()


# --- 조회 ---


async def test_list_events_asks_for_expanded_single_events():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=events_payload(ALL_DAY, TIMED))

    client, http = client_with(handler)
    async with http:
        events = await client.list_events(kst(9, 18, 0), kst(9, 19, 0))

    assert "singleEvents=true" in seen["url"] and "orderBy=startTime" in seen["url"]
    assert "timeZone=Asia%2FSeoul" in seen["url"]
    assert seen["auth"] == "Bearer token-1"
    assert [event.title for event in events] == ["개교기념일", "자료구조"]


async def test_other_calendar_id_is_used():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        return httpx.Response(200, json=events_payload())

    client, http = client_with(handler, "team@group.calendar.google.com")
    async with http:
        await client.events_on(kst(9, 18, 0).date())
    assert "team@group.calendar.google.com" in seen["path"]


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(401, json={}), GoogleAuthError),
        (httpx.Response(403, json={}), TransientGoogleError),
        (httpx.Response(429, json={}), TransientGoogleError),
        (httpx.Response(500, text="oops"), TransientGoogleError),
        (httpx.Response(404, json={}), GoogleApiError),
        (httpx.Response(400, json={}), GoogleApiError),
        (httpx.Response(200, text="not json"), GoogleApiError),
    ],
)
async def test_error_mapping(response, expected):
    client, http = client_with(lambda request: response)
    async with http:
        with pytest.raises(expected):
            await client.list_events(kst(9, 18, 0), kst(9, 19, 0))


async def test_connection_failure_is_transient():
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    client, http = client_with(handler)
    async with http:
        with pytest.raises(TransientGoogleError):
            await client.list_events(kst(9, 18, 0), kst(9, 19, 0))


# --- 쓰기 ---


async def test_create_event_sends_seoul_times():
    sent = {}

    def handler(request):
        sent["method"] = request.method
        sent["body"] = json.loads(request.content)
        return httpx.Response(200, json=TIMED)

    client, http = client_with(handler)
    async with http:
        created = await client.create_event("자료구조", kst(9, 18, 9), kst(9, 18, 10, 30), location="공학관 301")

    assert sent["method"] == "POST"
    assert sent["body"]["summary"] == "자료구조" and sent["body"]["location"] == "공학관 301"
    assert sent["body"]["start"] == {"dateTime": "2026-09-18T09:00:00+09:00", "timeZone": "Asia/Seoul"}
    assert created.id == "evt-1"


async def test_create_all_day_event_uses_exclusive_end():
    sent = {}

    def handler(request):
        sent["body"] = json.loads(request.content)
        return httpx.Response(200, json=ALL_DAY)

    client, http = client_with(handler)
    async with http:
        await client.create_event("개교기념일", kst(9, 18, 0), kst(9, 18, 23), all_day=True)
    assert sent["body"]["start"] == {"date": "2026-09-18"} and sent["body"]["end"] == {"date": "2026-09-19"}


async def test_update_sends_only_changed_fields():
    sent = {}

    def handler(request):
        sent["method"] = request.method
        sent["body"] = json.loads(request.content)
        return httpx.Response(200, json=TIMED | {"summary": "자료구조 보강"})

    client, http = client_with(handler)
    async with http:
        updated = await client.update_event("evt-1", title="자료구조 보강")
    assert sent["method"] == "PATCH" and sent["body"] == {"summary": "자료구조 보강"}
    assert updated.title == "자료구조 보강"


async def test_update_without_changes_is_an_error():
    client, http = client_with(lambda request: httpx.Response(200, json=TIMED))
    async with http:
        with pytest.raises(GoogleApiError):
            await client.update_event("evt-1")


@pytest.mark.parametrize("status", [204, 410])
async def test_delete_accepts_empty_and_already_gone(status):
    client, http = client_with(lambda request: httpx.Response(status))
    async with http:
        await client.delete_event("evt-1")


# --- 빈 시간과 겹침 ---


def test_free_slots_between_events():
    events = [parse_event(TIMED), parse_event(TIMED | {"start": {"dateTime": "2026-09-18T14:00:00+09:00"},
                                                       "end": {"dateTime": "2026-09-18T16:00:00+09:00"}})]
    slots = free_slots(events, kst(9, 18, 9).date(), 60)
    assert [(f"{begin:%H:%M}", f"{finish:%H:%M}") for begin, finish in slots] == [
        ("10:30", "14:00"),
        ("16:00", "22:00"),
    ]
    # 가장 긴 빈 구간(16:00~22:00)보다 더 긴 시간을 찾으면 결과가 없다
    assert free_slots(events, kst(9, 18, 9).date(), 400) == []


def test_all_day_event_fills_the_window():
    assert free_slots([parse_event(ALL_DAY)], kst(9, 18, 9).date(), 30) == []


def test_free_slots_ignores_other_days():
    assert len(free_slots([], kst(9, 18, 9).date(), 60)) == 1


def test_overlapping_pairs_finds_clashes():
    first = parse_event(TIMED)
    second = parse_event(TIMED | {"id": "evt-3", "summary": "면접",
                                  "start": {"dateTime": "2026-09-18T10:00:00+09:00"},
                                  "end": {"dateTime": "2026-09-18T11:00:00+09:00"}})
    apart = parse_event(TIMED | {"id": "evt-4", "summary": "점심",
                                 "start": {"dateTime": "2026-09-18T12:00:00+09:00"},
                                 "end": {"dateTime": "2026-09-18T13:00:00+09:00"}})
    pairs = overlapping_pairs([first, second, apart, parse_event(ALL_DAY)])
    assert [(a.title, b.title) for a, b in pairs] == [("자료구조", "면접")]


# --- 계정 묶음 ---


@pytest.fixture
def three_accounts(tmp_path):
    client_file = tmp_path / "google_client.json"
    client_file.write_text(json.dumps(CLIENT_JSON), encoding="utf-8")
    accounts = []
    for index, label in enumerate(("개인", "학교", "기타"), start=1):
        token_file = tmp_path / f"google_token_{index}.json"
        if label != "기타":  # 기타 계정은 아직 로그인 전
            token_file.write_text(StoredToken("refresh", "access", kst(9, 18, 23)).to_json(), encoding="utf-8")
        accounts.append(GoogleAccountSettings(label, token_file, default=(label == "개인")))
    return GoogleSettings(client_file=client_file, accounts=tuple(accounts))


def accounts_with(settings, handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # 토큰 만료 판단을 고정 시계로 한다
    return GoogleAccounts(settings, http, clock=lambda: kst(9, 18, 12)), http


def test_connected_accounts_and_lookup(three_accounts):
    accounts, _ = accounts_with(three_accounts, lambda request: httpx.Response(200, json=events_payload()))
    assert [account.label for account in accounts.connected] == ["개인", "학교"]
    assert accounts.ready is True and accounts.multiple is True
    assert accounts.find("학교").label == "학교" and accounts.find("없음") is None
    assert accounts.default_account().label == "개인"
    assert "기타: 연결 안 됨" in accounts.status() and "개인: 연결됨 (기본)" in accounts.status()


async def test_events_are_merged_and_tagged_by_account(three_accounts):
    def handler(request):
        # 계정을 구분할 수 없으니 같은 응답을 주고, 태그만 확인한다
        return httpx.Response(200, json=events_payload(TIMED))

    accounts, http = accounts_with(three_accounts, handler)
    async with http:
        result = await accounts.events(kst(9, 18, 0), kst(9, 19, 0))
    assert [event.account for event in result.events] == ["개인", "학교"]
    assert result.failed == () and result.warning == ""


async def test_one_failing_account_does_not_hide_the_others(three_accounts):
    def handler(request):
        handler.calls = getattr(handler, "calls", 0) + 1
        if handler.calls == 1:
            return httpx.Response(500, text="down")
        return httpx.Response(200, json=events_payload(TIMED))

    accounts, http = accounts_with(three_accounts, handler)
    async with http:
        result = await accounts.events(kst(9, 18, 0), kst(9, 19, 0))
    assert [event.account for event in result.events] == ["학교"]
    assert result.failed == ("개인",) and "개인" in result.warning


async def test_events_can_be_limited_to_one_account(three_accounts):
    accounts, http = accounts_with(three_accounts, lambda request: httpx.Response(200, json=events_payload(TIMED)))
    async with http:
        result = await accounts.events(kst(9, 18, 0), kst(9, 19, 0), "학교")
    assert [event.account for event in result.events] == ["학교"]


async def test_asking_a_disconnected_account_reports_it(three_accounts):
    accounts, http = accounts_with(three_accounts, lambda request: httpx.Response(200, json=events_payload()))
    async with http:
        result = await accounts.events(kst(9, 18, 0), kst(9, 19, 0), "기타")
    assert result.events == [] and result.failed == ("기타",)
