"""게시판 소스 확인: 공지·자료·쪽지를 한 파서로 읽어 항목과 알림으로 바꾼다."""

from datetime import timedelta

import pytest

from app.collectors.eclass.parse import CourseRow, parse_list
from app.collectors.eclass.scope import ScopeEntry, ScopeStore
from app.collectors.eclass.session import EclassError, Failure
from app.collectors.eclass.sources.board import BoardSource
from app.collectors.eclass.sources.build import build_sources, source_key, source_label
from app.core.config import Level
from app.core.events import EventKind
from app.storage.eclass import EclassRepository, ItemChange
from tests.conftest import kst

NOW = kst(9, 20, 9)

# 실제 화면과 같은 모양 (docs/refs/eclass-paths.md)
NOTICE_HTML = """
<table>
  <tr><th>번호</th><th>중요</th><th>제목</th><th>첨부</th><th>공개일</th></tr>
  <tr>
    <td class="number">2</td>
    <td class="center impt impt_off" impt_seq="6770111"></td>
    <td class="left" onclick="pageMove('/ilos/st/course/notice_view_form.acl?ARTL_NUM=6770111&amp;SCH_KEY='); return false;">
      <a class="site-link"><div>9월 23일 수업 휴강</div>
      <div class="subjt_bottom"><span>홍길동</span><span>조회 32</span></div></a>
    </td>
    <td>&nbsp;</td>
    <td class="number">2026.09.17 오전 9:30</td>
  </tr>
  <tr class="list">
    <td class="number">1</td>
    <td class="center impt impt_off" impt_seq="6758671"></td>
    <td class="left" onclick="pageMove('/ilos/st/course/notice_view_form.acl?ARTL_NUM=6758671'); return false;">
      <a class="site-link"><div>시험에 관하여</div></a>
    </td>
    <td>&nbsp;</td>
    <td class="number">2026.09.12 오후 12:00</td>
  </tr>
</table>
"""

MESSAGE_HTML = """
<table>
  <tr><th>보낸사람</th><th>제목</th><th>날짜</th></tr>
  <tr>
    <td class="chk"><input type="checkbox" value="1902274"></td>
    <td>홍길동</td>
    <td class="left"><a href="javascript:viewPage('1902274', 'ABC');">쪽지를 보냈습니다.</a></td>
    <td class="number last">09.17 오후 1:52</td>
  </tr>
</table>
"""

EMPTY_HTML = """
<table>
  <tr><th>번호</th><th>제목</th></tr>
  <tr><td colspan="2">조회할 자료가 없습니다</td></tr>
</table>
"""

BODY_HTML = '<div class="textviewer">9월 23일 수업은 휴강하고 온라인으로 보강합니다.</div>'


class FakeSession:
    def __init__(self, pages: dict[str, str], broken: set[str] | None = None) -> None:
        self.pages = pages
        self.broken = broken or set()
        self.opened: list[str] = []
        self.entered: list[str] = []

    async def open(self, path: str) -> str:
        self.opened.append(path)
        if path.split("?")[0] in self.broken:
            raise EclassError(Failure.NETWORK, "연결 실패")
        return self.pages.get(path.split("?")[0], EMPTY_HTML)

    async def post(self, path: str, data: dict) -> str:
        return await self.open(path)

    async def enter_course(self, key: str) -> None:
        if key in self.broken:
            raise EclassError(Failure.NETWORK, "연결 실패")
        self.entered.append(key)


COURSES = [CourseRow(kjkey="KJ1", name="자료구조"), CourseRow(kjkey="KJ2", name="운영체제")]


def source(**overrides) -> BoardSource:
    fields = {
        "key": "course_notice_list",
        "label": "공지사항",
        "path": "/ilos/st/course/notice_list.acl",
        "level": Level.NOTIFY,
        "clock": lambda: NOW,
    }
    return BoardSource(**(fields | overrides))


# --- 읽기 ---


async def test_a_board_becomes_items():
    session = FakeSession({"/ilos/st/course/notice_list.acl": NOTICE_HTML})
    result = await source().fetch(session, [])

    assert [item.title for item in result.items] == ["9월 23일 수업 휴강", "시험에 관하여"]
    first = result.items[0]
    assert first.item_id == "eclass:course_notice_list:common:6770111"
    assert first.kind == "공지사항" and first.posted_at == kst(9, 17, 9, 30)
    assert first.source == "course_notice_list"


async def test_a_shell_screen_is_read_from_its_content_address():
    session = FakeSession(
        {
            "/ilos/community/notice_list_form.acl": "<div>껍데기</div>",
            "/ilos/community/notice_list.acl": NOTICE_HTML,
        }
    )
    board = source(
        key="community_notice_list",
        path="/ilos/community/notice_list_form.acl",
        data_path="/ilos/community/notice_list.acl",
    )
    result = await board.fetch(session, [])
    assert len(result.items) == 2


async def test_a_course_board_is_read_in_each_room():
    session = FakeSession({"/ilos/st/course/notice_list.acl": NOTICE_HTML})
    result = await source(per_course=True).fetch(session, COURSES)

    assert session.entered == ["KJ1", "KJ2"]
    assert len(result.items) == 4
    # 같은 글번호라도 과목이 다르면 다른 항목이다
    assert result.items[0].item_id != result.items[2].item_id
    assert {item.course for item in result.items} == {"자료구조", "운영체제"}


async def test_one_locked_course_does_not_stop_the_rest():
    session = FakeSession({"/ilos/st/course/notice_list.acl": NOTICE_HTML}, broken={"KJ1"})
    result = await source(per_course=True).fetch(session, COURSES)

    assert session.entered == ["KJ2"]
    assert {item.course for item in result.items} == {"운영체제"}
    assert result.suspicious is False


async def test_every_course_failing_looks_like_a_changed_site():
    session = FakeSession({}, broken={"KJ1", "KJ2"})
    result = await source(per_course=True).fetch(session, COURSES)
    assert result.items == [] and result.suspicious is True


async def test_an_empty_board_is_not_a_failure():
    session = FakeSession({"/ilos/st/course/notice_list.acl": EMPTY_HTML})
    result = await source().fetch(session, [])
    assert result.items == [] and result.suspicious is False


async def test_a_message_list_is_read_the_same_way():
    """쪽지함은 글번호 대신 확인란을 쓰고 날짜에 연도가 없다."""
    session = FakeSession({"/ilos/message/received_list_pop_form.acl": MESSAGE_HTML})
    board = source(key="message_received_list_pop", label="쪽지", path="/ilos/message/received_list_pop_form.acl")
    result = await board.fetch(session, [])

    assert len(result.items) == 1
    assert result.items[0].item_id.endswith(":1902274")
    assert result.items[0].posted_at == kst(9, 17, 13, 52)


# --- 본문 ---


async def test_the_body_of_a_new_notice_is_read(db):
    session = FakeSession(
        {"/ilos/st/course/notice_list.acl": NOTICE_HTML, "/ilos/st/course/notice_view_form.acl": BODY_HTML}
    )
    result = await source(items=EclassRepository(db)).fetch(session, [])
    assert "온라인으로 보강" in result.items[0].body


async def test_an_article_we_already_read_is_not_opened_again(db):
    items = EclassRepository(db)
    session = FakeSession(
        {"/ilos/st/course/notice_list.acl": NOTICE_HTML, "/ilos/st/course/notice_view_form.acl": BODY_HTML}
    )
    board = source(items=items)
    for item in (await board.fetch(session, [])).items:
        await items.upsert(item, NOW)

    again = FakeSession(
        {"/ilos/st/course/notice_list.acl": NOTICE_HTML, "/ilos/st/course/notice_view_form.acl": BODY_HTML}
    )
    await board.fetch(again, [])
    assert not any("notice_view_form" in path for path in again.opened)


async def test_only_a_few_bodies_are_read_at_a_time(db):
    session = FakeSession(
        {"/ilos/st/course/notice_list.acl": NOTICE_HTML, "/ilos/st/course/notice_view_form.acl": BODY_HTML}
    )
    await source(items=EclassRepository(db), max_bodies=1).fetch(session, [])
    assert sum("notice_view_form" in path for path in session.opened) == 1


async def test_a_board_we_only_store_does_not_open_articles(db):
    session = FakeSession(
        {"/ilos/st/course/notice_list.acl": NOTICE_HTML, "/ilos/st/course/notice_view_form.acl": BODY_HTML}
    )
    await source(level=Level.STORE, items=EclassRepository(db)).fetch(session, [])
    assert not any("notice_view_form" in path for path in session.opened)


async def test_a_body_that_will_not_load_is_left_empty(db):
    session = FakeSession(
        {"/ilos/st/course/notice_list.acl": NOTICE_HTML}, broken={"/ilos/st/course/notice_view_form.acl"}
    )
    result = await source(items=EclassRepository(db)).fetch(session, [])
    assert result.items[0].body == ""


# --- 알림 ---


async def test_a_new_notice_is_told_about(db):
    session = FakeSession({"/ilos/st/course/notice_list.acl": NOTICE_HTML})
    board = source(per_course=True)
    item = (await board.fetch(session, COURSES[:1])).items[0]

    event = board.event_for(item, ItemChange.NEW, NOW)
    assert event.kind == EventKind.NOTICE
    assert event.title == "공지사항: [자료구조] 9월 23일 수업 휴강"
    assert event.urgent is False and event.ref_id == item.item_id


@pytest.mark.parametrize("change", [ItemChange.SAME, ItemChange.DUE_CHANGED])
async def test_an_item_we_already_saw_is_quiet(change, db):
    session = FakeSession({"/ilos/st/course/notice_list.acl": NOTICE_HTML})
    board = source()
    item = (await board.fetch(session, [])).items[0]
    assert board.event_for(item, change, NOW) is None


async def test_a_board_we_only_store_never_tells_us(db):
    session = FakeSession({"/ilos/st/course/notice_list.acl": NOTICE_HTML})
    board = source(level=Level.STORE)
    item = (await board.fetch(session, [])).items[0]
    assert board.event_for(item, ItemChange.NEW, NOW) is None


# --- 소스 만들기 ---


def test_a_source_name_tells_two_similar_screens_apart():
    assert source_key("/ilos/st/course/notice_list.acl") == "course_notice_list"
    assert source_key("/ilos/community/notice_list_form.acl") == "community_notice_list"


def test_a_menu_name_with_an_unread_count_is_cleaned():
    assert source_label("공지사항 1", "course_notice_list") == "공지사항"
    assert source_label("", "message_received_list_pop") == "쪽지"


def catalog(tmp_path, rows) -> object:
    import json

    path = tmp_path / "eclass_catalog.json"
    path.write_text(json.dumps({"screens": rows}, ensure_ascii=False), encoding="utf-8")
    return path


def test_sources_are_built_from_the_catalog_and_the_scope(tmp_path):
    path = catalog(
        tmp_path,
        [
            {"path": "/ilos/community/notice_list_form.acl", "name": "공지", "data_path": "/ilos/community/notice_list.acl"},
            {"path": "/ilos/community/notice_list.acl", "name": ""},
            {"path": "/ilos/guide/guide_main_form.acl", "name": "FAQ"},
            {"path": "/ilos/mp/todo_list_form.acl", "name": "할 일"},
        ],
    )
    scope = {
        "/ilos/community/notice_list_form.acl": ScopeEntry(
            path="/ilos/community/notice_list_form.acl", name="공지", level=Level.NOTIFY
        ),
        "/ilos/community/notice_list.acl": ScopeEntry(
            path="/ilos/community/notice_list.acl", name="", level=Level.NOTIFY
        ),
        "/ilos/guide/guide_main_form.acl": ScopeEntry(
            path="/ilos/guide/guide_main_form.acl", name="FAQ", level=Level.OFF
        ),
        "/ilos/mp/todo_list_form.acl": ScopeEntry(
            path="/ilos/mp/todo_list_form.acl", name="할 일", level=Level.NOTIFY
        ),
    }
    sources = build_sources(path, scope)
    keys = [item.key for item in sources]

    # 할 일은 언제나 첫 소스이고, 그 화면으로 게시판 소스를 또 만들지 않는다
    assert keys[0] == "todo" and keys.count("todo") == 1
    assert "mp_todo_list" not in keys
    # 껍데기가 부르는 내용 주소는 그 자체로 소스가 아니다
    assert keys == ["todo", "community_notice_list"]
    # 꺼 둔 화면은 빠진다
    assert not any("guide" in key for key in keys)


def test_a_course_screen_is_marked_even_without_a_course_key(tmp_path):
    path = catalog(tmp_path, [{"path": "/ilos/st/course/report_list.acl", "name": "과제"}])
    scope = {
        "/ilos/st/course/report_list.acl": ScopeEntry(
            path="/ilos/st/course/report_list.acl", name="과제", level=Level.STORE
        )
    }
    board = build_sources(path, scope)[1]
    assert board.per_course is True and board.interval == timedelta(hours=12)


def test_what_we_only_store_is_looked_at_less_often(tmp_path):
    path = catalog(
        tmp_path,
        [
            {"path": "/ilos/a/notice_list.acl", "name": "공지"},
            {"path": "/ilos/b/material_list.acl", "name": "자료"},
        ],
    )
    scope = {
        "/ilos/a/notice_list.acl": ScopeEntry(path="/ilos/a/notice_list.acl", name="공지", level=Level.NOTIFY),
        "/ilos/b/material_list.acl": ScopeEntry(path="/ilos/b/material_list.acl", name="자료", level=Level.STORE),
    }
    by_key = {item.key: item for item in build_sources(path, scope)}
    assert by_key["a_notice_list"].interval == timedelta(0)
    assert by_key["a_notice_list"].priority < by_key["b_material_list"].priority


def test_without_a_catalog_only_the_todo_screen_is_read(tmp_path):
    assert [item.key for item in build_sources(tmp_path / "없음.json", {})] == ["todo"]


# --- 파서 ---


def test_a_row_without_a_number_or_a_title_is_skipped():
    result = parse_list("<table><tr><td>가</td><td>나</td></tr></table>", NOW)
    assert result.rows == [] and result.skipped == 1


def test_the_same_article_is_listed_once():
    doubled = NOTICE_HTML.replace("6758671", "6770111")
    result = parse_list(doubled, NOW)
    assert len(result.rows) == 1 and result.skipped == 1


def test_a_date_without_a_year_that_is_far_ahead_is_last_year():
    """9월에 12월 날짜가 보이면 지난해 글이다."""
    html = MESSAGE_HTML.replace("09.17 오후 1:52", "12.20 오후 1:52")
    assert parse_list(html, NOW).rows[0].posted_at == kst(12, 20, 13, 52).replace(year=2025)
