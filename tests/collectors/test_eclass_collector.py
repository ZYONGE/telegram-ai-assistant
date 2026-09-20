from datetime import datetime, timedelta

import pytest

from app.collectors.eclass.collector import BLOCKED_MESSAGE, EclassCollector
from app.collectors.eclass.session import EclassError, Failure
from app.core.config import EclassSettings
from app.collectors.eclass.sources import SourceResult
from app.core.events import Event, EventKind, EventSource
from app.storage.eclass import (
    EclassHealthStore,
    EclassItem,
    EclassRepository,
    EclassSourceStateStore,
    ItemChange,
)
from tests.conftest import kst

NOW = kst(9, 20, 9)
SETTINGS = EclassSettings(
    eclass_url="https://eclass.example.ac.kr/", username="학번", password="비밀", poll_minutes=90
)


def todo_html(*rows: str) -> str:
    return f'<div class="todo_list">{"".join(rows)}<div class="todo_wrap no_data">할 일이 없습니다</div></div>'


def todo_row(title: str, due: str, seq: str = "7", gubun: str = "report", course: str = "자료구조") -> str:
    return f"""
    <div class="todo_wrap" onclick="goLecture('KJ1','{seq}','{gubun}')">
      <div class="todo_subjt">{course}</div>
      <div class="todo_title">{title}</div>
      <div class="todo_date">{due}</div>
    </div>
    """


class FakeSession:
    """세션 계층을 흉내 낸다. 브라우저 없이 수집기 흐름만 본다."""

    def __init__(self, html: str = "", error: EclassError | None = None) -> None:
        self.html = html
        self.error = error
        self.closed = False
        self.logged_in = False

    def __call__(self, settings):  # session_factory 자리
        return self

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    async def ensure_login(self) -> None:
        if self.error:
            raise self.error
        self.logged_in = True

    async def open(self, path: str) -> str:
        return "<html></html>"

    async def post(self, path: str, data: dict) -> str:
        return self.html


@pytest.fixture
def stores(db):
    return EclassRepository(db), EclassHealthStore(db), EclassSourceStateStore(db)


def collector(
    stores,
    session,
    settings: EclassSettings = SETTINGS,
    sources=None,
    now: datetime = NOW,
) -> EclassCollector:
    items, health, state = stores
    return EclassCollector(settings, items, health, state, lambda: now, session, sources)


# --- 정상 수집 ---


async def test_new_assignment_becomes_a_deadline_event(stores):
    session = FakeSession(todo_html(todo_row("과제 2 제출", "2026.09.25 오후 11:59")))
    events = await collector(stores, session).collect()

    assert len(events) == 1
    event = events[0]
    assert event.source == EventSource.ECLASS and event.kind == EventKind.DEADLINE
    assert event.title == "[자료구조] 과제 2 제출"
    assert event.due_at == kst(9, 25, 23, 59) and event.urgent is False
    assert event.ref_id == "eclass:과제:KJ1:7"
    assert session.logged_in and session.closed


async def test_deadline_within_a_day_is_urgent(stores):
    session = FakeSession(todo_html(todo_row("퀴즈", "2026.09.20 오후 11:59", gubun="test")))
    events = await collector(stores, session).collect()
    assert events[0].urgent is True and events[0].meta["category"] == "시험"


async def test_same_item_is_quiet_on_the_next_run(stores):
    session = FakeSession(todo_html(todo_row("과제 2 제출", "2026.09.25 오후 11:59")))
    assert len(await collector(stores, session).collect()) == 1
    assert await collector(stores, session).collect() == []


async def test_changed_deadline_alerts_once(stores):
    first = FakeSession(todo_html(todo_row("과제 2 제출", "2026.09.25 오후 11:59")))
    await collector(stores, first).collect()

    changed = FakeSession(todo_html(todo_row("과제 2 제출", "2026.09.27 오후 11:59")))
    events = await collector(stores, changed).collect()
    assert len(events) == 1 and events[0].kind == EventKind.DEADLINE_CHANGED
    assert events[0].urgent is True and "9월 27일" in events[0].body
    # 같은 상태로 한 번 더 돌아도 조용하다
    assert await collector(stores, changed).collect() == []


async def test_item_without_a_due_date_is_a_notice(stores):
    session = FakeSession(todo_html(todo_row("공지 확인", "마감 없음")))
    events = await collector(stores, session).collect()
    assert events[0].kind == EventKind.NOTICE and events[0].due_at is None


async def test_empty_list_is_not_a_failure(stores):
    session = FakeSession(todo_html())
    assert await collector(stores, session).collect() == []
    assert (await stores[1].read()).last_ok_at == NOW


# --- 실패 처리 ---


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        (Failure.LOGIN, "login"),
        (Failure.CAPTCHA, "captcha"),
        (Failure.NETWORK, "network"),
    ],
)
async def test_failures_are_reported_as_collector_failed(stores, failure, expected_reason):
    session = FakeSession(error=EclassError(failure, "그런 이유로 실패"))
    events = await collector(stores, session).collect()

    assert len(events) == 1 and events[0].kind == EventKind.COLLECTOR_FAILED
    assert events[0].meta["reason"] == expected_reason
    assert events[0].ref_id.endswith(expected_reason)
    assert session.closed


async def test_two_login_failures_stop_the_automation(stores):
    session = FakeSession(error=EclassError(Failure.LOGIN, "로그인 실패"))
    first = await collector(stores, session).collect()
    assert BLOCKED_MESSAGE not in first[0].body

    second = await collector(stores, session).collect()
    assert BLOCKED_MESSAGE in second[0].body

    # 세 번째부터는 아예 시도하지 않는다
    quiet_session = FakeSession(error=EclassError(Failure.LOGIN, "로그인 실패"))
    assert await collector(stores, quiet_session).collect() == []
    assert quiet_session.logged_in is False


async def test_user_can_resume_after_fixing_the_password(stores):
    session = FakeSession(error=EclassError(Failure.LOGIN, "로그인 실패"))
    await collector(stores, session).collect()
    await collector(stores, session).collect()

    await stores[1].clear()
    working = FakeSession(todo_html(todo_row("과제 2 제출", "2026.09.25 오후 11:59")))
    assert len(await collector(stores, working).collect()) == 1


async def test_broken_layout_is_reported(stores):
    # todo_wrap은 있는데 필수 항목이 비어 있으면 구조 변경을 의심한다
    session = FakeSession('<div class="todo_wrap"><div class="todo_title">제목뿐</div></div>')
    events = await collector(stores, session).collect()
    assert events[0].meta["reason"] == "layout"


async def test_long_silence_adds_a_second_alert(stores):
    _items, health, _state = stores
    await health.record_success(kst(9, 18, 9))
    session = FakeSession(error=EclassError(Failure.NETWORK, "연결 실패"))

    events = await collector(stores, session).collect()
    reasons = [event.meta["reason"] for event in events]
    assert reasons == ["network", "stale"]
    assert "마지막 확인" in events[1].body


async def test_disabled_settings_do_nothing(stores):
    session = FakeSession(todo_html(todo_row("과제", "2026.09.25 오후 11:59")))
    off = EclassSettings(eclass_url="", username="", password="")
    assert await collector(stores, session, off).collect() == []
    assert session.logged_in is False


# --- 소스 여러 개 ---


class FakeSource:
    """화면 하나를 흉내 낸다. 정해진 항목을 돌려주거나 실패한다."""

    def __init__(
        self,
        key: str,
        *,
        items: list[EclassItem] | None = None,
        error: EclassError | None = None,
        interval: timedelta = timedelta(0),
    ) -> None:
        self.key = key
        self.label = key
        self.interval = interval
        self.calls = 0
        self._items = items or []
        self._error = error

    async def fetch(self, session, courses) -> SourceResult:
        self.calls += 1
        if self._error:
            raise self._error
        return SourceResult(list(self._items))

    def event_for(self, item: EclassItem, change: ItemChange, now: datetime) -> Event | None:
        if change is ItemChange.SAME:
            return None
        return Event(
            source=EventSource.ECLASS, kind=EventKind.NOTICE, title=item.title, ref_id=item.item_id
        )


def item(item_id: str, title: str) -> EclassItem:
    return EclassItem(item_id=item_id, kind="공지", title=title)


async def test_one_broken_source_does_not_stop_the_others(stores):
    broken = FakeSource("notice", error=EclassError(Failure.LAYOUT, "읽지 못했습니다"))
    working = FakeSource("message", items=[item("eclass:msg:1", "쪽지 한 통")])

    events = await collector(stores, FakeSession(), sources=[broken, working]).collect()

    assert "쪽지 한 통" in [event.title for event in events]
    assert working.calls == 1
    failure = next(event for event in events if event.kind == EventKind.COLLECTOR_FAILED)
    # 어느 화면이 막혔는지 이름을 붙여 알린다
    assert failure.meta["reason"] == "notice:layout" and "notice" in failure.body
    # 한 소스가 막혔다고 수집기가 멈춘 것은 아니다
    assert (await stores[1].read()).fail_count == 0


async def test_every_source_failing_is_a_collector_failure(stores):
    one = FakeSource("notice", error=EclassError(Failure.LAYOUT, "읽지 못했습니다"))
    two = FakeSource("message", error=EclassError(Failure.NETWORK, "연결 실패"))

    events = await collector(stores, FakeSession(), sources=[one, two]).collect()

    assert len(events) == 1 and events[0].meta["reason"] == "layout"
    assert (await stores[1].read()).fail_count == 1


async def test_a_slow_source_waits_for_its_turn(stores):
    daily = FakeSource(
        "syllabus", items=[item("eclass:syllabus:1", "강의계획서")], interval=timedelta(hours=24)
    )
    assert len(await collector(stores, FakeSession(), sources=[daily]).collect()) == 1

    # 한 시간 뒤에는 아직 차례가 아니다. 로그인조차 하지 않는다.
    quiet = FakeSession()
    later = collector(stores, quiet, sources=[daily], now=NOW + timedelta(hours=1))
    assert await later.collect() == []
    assert daily.calls == 1 and quiet.logged_in is False

    # 하루가 지나면 다시 본다
    await collector(stores, FakeSession(), sources=[daily], now=NOW + timedelta(hours=24)).collect()
    assert daily.calls == 2


async def test_a_failed_source_is_tried_again_next_turn(stores):
    flaky = FakeSource("notice", error=EclassError(Failure.NETWORK, "연결 실패"))
    working = FakeSource("message", items=[item("eclass:msg:1", "쪽지 한 통")])

    await collector(stores, FakeSession(), sources=[flaky, working]).collect()
    await collector(stores, FakeSession(), sources=[flaky, working]).collect()
    assert flaky.calls == 2
