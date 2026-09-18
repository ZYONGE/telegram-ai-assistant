import json

import httpx
import pytest

from app.core.config import GoogleAccountSettings, GoogleSettings
from app.core.interfaces import Confirmation
from app.google.accounts import GoogleAccounts
from app.google.auth import StoredToken
from app.tools.calendar import NOT_CONNECTED, calendar_tools, not_connected_tools
from app.tools.common import ToolInputError
from tests.conftest import kst
from tests.google.test_auth import CLIENT_JSON

NOW = kst(9, 18, 8)


def event(summary: str, start: str, end: str, event_id: str = "evt-1", location: str = "") -> dict:
    return {
        "id": event_id,
        "summary": summary,
        "location": location,
        "start": {"dateTime": f"2026-09-18T{start}:00+09:00"},
        "end": {"dateTime": f"2026-09-18T{end}:00+09:00"},
    }


class FakeCalendarApi:
    """계정별 일정을 흉내 내는 가짜 캘린더. 계정 구분은 토큰 파일 경로 대신 호출 순서로 한다."""

    def __init__(self, per_account: list[list[dict]]) -> None:
        self.per_account = per_account
        self.reads = 0
        self.writes: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            items = self.per_account[min(self.reads, len(self.per_account) - 1)]
            self.reads += 1
            return httpx.Response(200, json={"items": items})
        if request.method == "DELETE":
            self.writes.append(("DELETE", {}))
            return httpx.Response(204)
        body = json.loads(request.content)
        self.writes.append((request.method, body))
        merged = event(body.get("summary", "새 일정"), "15:00", "16:00")
        return httpx.Response(200, json=merged | {k: v for k, v in body.items() if k == "summary"})


def build(tmp_path, api: FakeCalendarApi, connected=("개인", "학교")):
    client_file = tmp_path / "google_client.json"
    client_file.write_text(json.dumps(CLIENT_JSON), encoding="utf-8")
    settings_accounts = []
    for index, label in enumerate(("개인", "학교"), start=1):
        token_file = tmp_path / f"token_{index}.json"
        if label in connected:
            token_file.write_text(StoredToken("r", "access", kst(9, 18, 23)).to_json(), encoding="utf-8")
        settings_accounts.append(GoogleAccountSettings(label, token_file, default=(label == "개인")))
    settings = GoogleSettings(client_file=client_file, accounts=tuple(settings_accounts))
    http = httpx.AsyncClient(transport=httpx.MockTransport(api))
    accounts = GoogleAccounts(settings, http)
    return {tool.spec.name: tool for tool in calendar_tools(accounts, lambda: NOW)}, http


# --- 조회 ---


async def test_list_events_merges_accounts_and_marks_them(tmp_path):
    api = FakeCalendarApi([[event("자료구조", "09:00", "10:30")], [event("동아리", "19:00", "21:00", "evt-2")]])
    tools, http = build(tmp_path, api)
    async with http:
        result = await tools["list_events"].run({})
    assert result.content == "[개인] 09:00~10:30 자료구조\n[학교] 19:00~21:00 동아리"


async def test_list_events_for_one_account(tmp_path):
    api = FakeCalendarApi([[event("동아리", "19:00", "21:00")]])
    tools, http = build(tmp_path, api)
    async with http:
        result = await tools["list_events"].run({"account": "학교"})
    assert api.reads == 1 and "동아리" in result.content


async def test_empty_day_says_so_and_reports_failures(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        handler.calls = getattr(handler, "calls", 0) + 1
        return httpx.Response(200, json={"items": []}) if handler.calls == 1 else httpx.Response(500, text="down")

    tools, http = build(tmp_path, handler)
    async with http:
        result = await tools["list_events"].run({"start": "내일"})
    assert "등록된 일정이 없습니다" in result.content and "학교" in result.content


async def test_multi_day_listing_shows_dates(tmp_path):
    api = FakeCalendarApi([[event("자료구조", "09:00", "10:30")], []])
    tools, http = build(tmp_path, api)
    async with http:
        result = await tools["list_events"].run({"days": 3})
    assert result.content.startswith("[개인] 9월 18일(금) 09:00~10:30")


@pytest.mark.parametrize("days", [0, 15, "셋"])
async def test_bad_day_count_is_rejected(tmp_path, days):
    tools, http = build(tmp_path, FakeCalendarApi([[]]))
    async with http:
        with pytest.raises(ToolInputError):
            await tools["list_events"].run({"days": days})


async def test_unknown_date_is_rejected(tmp_path):
    tools, http = build(tmp_path, FakeCalendarApi([[]]))
    async with http:
        with pytest.raises(ToolInputError, match="날짜를 알아볼 수 없습니다"):
            await tools["list_events"].run({"start": "다음다음주쯤"})


async def test_find_free_time_uses_every_account(tmp_path):
    api = FakeCalendarApi([[event("자료구조", "09:00", "12:00")], [event("동아리", "13:00", "15:00", "evt-2")]])
    tools, http = build(tmp_path, api)
    async with http:
        result = await tools["find_free_time"].run({"minutes": 60})
    assert api.reads == 2
    assert result.content.splitlines()[1:] == ["12:00~13:00", "15:00~22:00"]


# --- 등록 ---


async def test_add_event_confirms_before_writing(tmp_path):
    api = FakeCalendarApi([[event("면접", "15:30", "16:30")], []])
    tools, http = build(tmp_path, api)
    async with http:
        summary = await tools["add_event"].describe({"title": "스터디", "start": "2026-09-18T15:00", "minutes": 90})
        assert summary.startswith("일정 등록 — [개인] 9월 18일(금) 15:00~16:30 스터디")
        # 겹치는 일정이 있으면 확인 문구에서 알려 준다
        assert "겹치는 일정: [개인] 15:30~16:30 면접" in summary
        assert api.writes == []

        result = await tools["add_event"].run({"title": "스터디", "start": "2026-09-18T15:00", "minutes": 90})
    method, body = api.writes[0]
    assert method == "POST" and body["summary"] == "스터디"
    assert body["start"]["dateTime"].startswith("2026-09-18T15:00")
    assert "등록했습니다" in result.content


async def test_add_event_goes_to_the_chosen_account(tmp_path):
    api = FakeCalendarApi([[], []])
    tools, http = build(tmp_path, api)
    async with http:
        summary = await tools["add_event"].describe(
            {"title": "면담", "start": "2026-09-18T11:00", "account": "학교"}
        )
    assert summary.startswith("일정 등록 — [학교]")


async def test_add_event_checks_times(tmp_path):
    tools, http = build(tmp_path, FakeCalendarApi([[], []]))
    async with http:
        with pytest.raises(ToolInputError, match="끝 시각"):
            await tools["add_event"].describe(
                {"title": "x", "start": "2026-09-18T15:00", "end": "2026-09-18T14:00"}
            )
        with pytest.raises(ToolInputError):
            await tools["add_event"].describe({"title": "x", "start": "언제쯤"})


async def test_writing_needs_a_connected_account(tmp_path):
    tools, http = build(tmp_path, FakeCalendarApi([[]]), connected=())
    async with http:
        with pytest.raises(ToolInputError, match="연결"):
            await tools["add_event"].describe({"title": "x", "start": "2026-09-18T15:00"})


# --- 수정과 삭제 ---


async def test_update_finds_the_event_by_day_and_title(tmp_path):
    api = FakeCalendarApi([[event("자료구조 강의", "09:00", "10:30")], []])
    tools, http = build(tmp_path, api)
    async with http:
        summary = await tools["update_event"].describe(
            {"day": "오늘", "title": "자료구조", "new_start": "2026-09-18T11:00"}
        )
        assert summary.startswith("일정 수정 — [개인] 09:00~10:30 자료구조 강의")
        assert "시작 → 9월 18일(금) 11:00" in summary

        api.reads = 0
        result = await tools["update_event"].run({"day": "오늘", "title": "자료구조", "new_title": "자료구조 보강"})
    method, body = api.writes[0]
    assert method == "PATCH" and body == {"summary": "자료구조 보강"}
    assert "수정했습니다" in result.content


async def test_update_without_changes_asks_for_them(tmp_path):
    api = FakeCalendarApi([[event("자료구조", "09:00", "10:30")], []])
    tools, http = build(tmp_path, api)
    async with http:
        with pytest.raises(ToolInputError, match="바꿀 내용"):
            await tools["update_event"].describe({"day": "오늘", "title": "자료구조"})


async def test_ambiguous_title_is_reported_with_candidates(tmp_path):
    api = FakeCalendarApi(
        [[event("스터디 준비", "09:00", "10:00"), event("스터디 모임", "19:00", "20:00", "evt-2")], []]
    )
    tools, http = build(tmp_path, api)
    async with http:
        with pytest.raises(ToolInputError, match="여러 개입니다") as error:
            await tools["delete_event"].describe({"day": "오늘", "title": "스터디"})
    assert "스터디 준비" in str(error.value) and "스터디 모임" in str(error.value)


async def test_missing_event_is_reported(tmp_path):
    tools, http = build(tmp_path, FakeCalendarApi([[], []]))
    async with http:
        with pytest.raises(ToolInputError, match="맞는 일정이 없습니다"):
            await tools["delete_event"].describe({"day": "오늘", "title": "없는 일정"})


async def test_delete_confirms_then_removes(tmp_path):
    api = FakeCalendarApi([[event("면접", "15:30", "16:30")], []])
    tools, http = build(tmp_path, api)
    async with http:
        summary = await tools["delete_event"].describe({"day": "오늘", "title": "면접"})
        assert summary == "일정 삭제 — [개인] 9월 18일(금) 15:30~16:30 면접"
        assert api.writes == []
        api.reads = 0
        result = await tools["delete_event"].run({"day": "오늘", "title": "면접"})
    assert api.writes[0][0] == "DELETE" and "삭제했습니다" in result.content


# --- 확인 단계와 미연결 상태 ---


def test_confirmation_levels(tmp_path):
    tools, _ = build(tmp_path, FakeCalendarApi([[]]))
    assert tools["list_events"].spec.confirmation is Confirmation.IMMEDIATE
    assert tools["find_free_time"].spec.confirmation is Confirmation.IMMEDIATE
    for name in ("add_event", "update_event", "delete_event"):
        assert tools[name].spec.confirmation is Confirmation.BUTTON


async def test_tools_exist_before_the_account_is_connected():
    tools = {tool.spec.name: tool for tool in not_connected_tools()}
    assert set(tools) == {"list_events", "find_free_time", "add_event", "update_event", "delete_event"}
    result = await tools["list_events"].run({})
    assert result.is_error is True and result.content == NOT_CONNECTED
