from datetime import datetime, timedelta

import pytest

from app.collectors.eclass.collector import (
    BLOCKED_MESSAGE,
    MAX_COURSE_SOURCES_PER_RUN,
    MAX_SOURCES_PER_RUN,
    EclassCollector,
)
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
    session = FakeSession(error=EclassError(Failure.LAYOUT, "구조가 바뀌었습니다"))

    events = await collector(stores, session).collect()
    reasons = [event.meta["reason"] for event in events]
    assert reasons == ["layout", "stale"]
    assert "마지막 확인" in events[1].body


# --- 잠깐 끊긴 것과 오래 끊긴 것 (docs/tasks.md T-28) ---


async def test_a_brief_disconnection_is_not_told_about(stores):
    """기기를 들고 다니면 인터넷이 잠깐씩 끊긴다. 그때마다 알리면 성가시다."""
    session = FakeSession(error=EclassError(Failure.NETWORK, "연결 실패"))
    assert await collector(stores, session).collect() == []


async def test_a_disconnection_that_keeps_happening_is_told_about(stores):
    session = FakeSession(error=EclassError(Failure.NETWORK, "연결 실패"))
    await collector(stores, session).collect()

    events = await collector(stores, session).collect()
    assert [event.meta["reason"] for event in events] == ["network"]


async def test_a_long_silence_is_told_about_even_while_quiet_about_blips(stores):
    """연결 문제를 참아 주더라도, 며칠째 확인이 안 되는 것은 알려야 한다."""
    _items, health, _state = stores
    await health.record_success(kst(9, 18, 9))
    session = FakeSession(error=EclassError(Failure.NETWORK, "연결 실패"))

    events = await collector(stores, session).collect()
    assert [event.meta["reason"] for event in events] == ["stale"]


async def test_other_failures_are_told_about_at_once(stores):
    """로그인·구조 변경은 사람이 고쳐야 하는 문제다. 미룰 까닭이 없다."""
    session = FakeSession(error=EclassError(Failure.LOGIN, "로그인 실패"))
    events = await collector(stores, session).collect()
    assert [event.meta["reason"] for event in events] == ["login"]


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
        per_course: bool = False,
    ) -> None:
        self.key = key
        self.label = key
        self.interval = interval
        self.per_course = per_course
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


# --- 화면을 처음 볼 때 ---


class DatedSource(FakeSource):
    """올린 시각이 있는 글을 돌려주는 소스 (게시판)."""

    def __init__(self, key: str, rows: list[tuple[str, datetime | None]]) -> None:
        super().__init__(key)
        self._rows = rows

    async def fetch(self, session, courses) -> SourceResult:
        self.calls += 1
        return SourceResult(
            [
                EclassItem(item_id=f"eclass:{self.key}:{name}", kind="공지", title=name, posted_at=when)
                for name, when in self._rows
            ]
        )


async def test_old_posts_are_taken_in_quietly_the_first_time(stores):
    """게시판을 처음 열면 지난 글이 잔뜩 있다. 그것까지 알리면 브리핑이 넘친다."""
    board = DatedSource("notice", [("작년 글", kst(1, 5, 9)), ("어제 글", NOW - timedelta(days=1))])
    events = await collector(stores, FakeSession(), sources=[board]).collect()

    assert [event.title for event in events] == ["어제 글"]
    # 알리지 않았을 뿐 담아 두기는 했다
    assert await stores[0].get("eclass:notice:작년 글") is not None


async def test_an_item_without_a_posting_time_is_still_told_about(stores):
    """할 일에는 올린 시각이 없다. 처음 수집이라고 빠뜨리면 할 일이 등록되지 않는다."""
    board = DatedSource("todo", [("과제", None)])
    events = await collector(stores, FakeSession(), sources=[board]).collect()
    assert [event.title for event in events] == ["과제"]


async def test_from_the_second_time_old_posts_are_told_about(stores):
    """한 번 본 화면에 뒤늦게 옛 글이 올라오면 그건 새 글이다."""
    session = FakeSession()
    await collector(stores, session, sources=[DatedSource("notice", [])]).collect()

    board = DatedSource("notice", [("뒤늦게 올라온 옛 글", kst(1, 5, 9))])
    events = await collector(stores, session, sources=[board]).collect()
    assert [event.title for event in events] == ["뒤늦게 올라온 옛 글"]


async def test_only_a_few_sources_run_at_a_time(stores):
    sources = [FakeSource(f"board{index}") for index in range(MAX_SOURCES_PER_RUN + 4)]
    await collector(stores, FakeSession(), sources=sources).collect()

    assert sum(source.calls for source in sources) == MAX_SOURCES_PER_RUN


async def test_few_course_screens_run_at_a_time(stores):
    """과목방 화면은 한 소스가 과목 수만큼 요청한다. 한 번에 몰아서 돌리지 않는다."""
    rooms = [FakeSource(f"room{index}", per_course=True) for index in range(5)]
    await collector(stores, FakeSession(), sources=rooms).collect()

    assert sum(source.calls for source in rooms) == MAX_COURSE_SOURCES_PER_RUN


async def test_the_screens_we_notify_about_go_first(stores):
    """한 번도 돌지 않은 소스끼리는 알릴 화면이 앞선다."""
    quiet = [FakeSource(f"stored{index}") for index in range(MAX_SOURCES_PER_RUN)]
    for source in quiet:
        source.priority = 2
    loud = FakeSource("notice")
    loud.priority = 0

    await collector(stores, FakeSession(), sources=[*quiet, loud]).collect()
    assert loud.calls == 1


async def test_the_source_that_waited_longest_goes_next(stores):
    """한 번에 다 돌리지 않으므로, 늘 같은 것만 돌면 나머지는 영영 밀린다."""
    sources = [FakeSource(f"board{index}") for index in range(MAX_SOURCES_PER_RUN + 3)]
    first = collector(stores, FakeSession(), sources=sources)

    await first.collect()
    ran_first = {source.key for source in sources if source.calls}
    await collector(stores, FakeSession(), sources=sources, now=NOW + timedelta(minutes=90)).collect()

    ran_second = {source.key for source in sources if source.calls and source.key not in ran_first}
    assert ran_second and not (ran_second & ran_first)
