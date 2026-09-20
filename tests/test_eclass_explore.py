"""eClass 탐색 스크립트 (scripts/eclass_explore.py) 확인.

실제 사이트에 붙지 않는다. 가짜 화면으로 판정과 안전 규칙만 본다.
실제 실행은 학교 계정이 있는 개발 PC에서 한다 (docs/tasks.md T-06).
"""

import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from app.collectors.eclass.session import EclassError, Failure  # noqa: E402
from scripts.eclass_explore import (  # noqa: E402
    Screen,
    collect_links,
    data_path,
    describe,
    explore,
    is_per_course,
    normalize,
    param_names,
    safe_path,
    sample_name,
    summary,
    write_catalog,
)

MAIN_HTML = """
<div id="menu">
  <a href="/ilos/community/notice_list_form.acl">공지사항</a>
  <a href="/ilos/mp/note_list_form.acl?KJKEY=ABC123">쪽지함</a>
  <div onclick="location.href='/ilos/st/schedule/academic_calendar_list_form.acl'">학사일정</div>
  <a href="/ilos/community/notice_write_form.acl">글쓰기</a>
  <a href="/ilos/lo/filedown.acl?FILE=1">첨부파일</a>
  <a href="/ilos/main/member/logout.acl">로그아웃</a>
  <a href="#">맨 위로</a>
  <a href="https://other.example.com/notice">외부 안내</a>
</div>
"""

NOTICE_HTML = """
<table>
  <thead><tr><th>번호</th><th>제목</th><th>등록일</th></tr></thead>
  <tbody>
    <tr><td>2</td><td><a href="/ilos/community/notice_view_form.acl?ARTL_NUM=124">시험 일정 변경</a></td><td>2026.09.20</td></tr>
    <tr><td>1</td><td><a href="/ilos/community/notice_view_form.acl?ARTL_NUM=123">휴강 안내</a></td><td>2026.09.19</td></tr>
  </tbody>
</table>
"""

PLAIN_HTML = "<div><h2>학사 안내</h2><p>안내문입니다.</p></div>"


class FakeSession:
    """경로를 주면 미리 정해 둔 화면을 돌려준다."""

    def __init__(self, pages: dict[str, str], broken: set[str] | None = None) -> None:
        self.pages = pages
        self.broken = broken or set()
        self.opened: list[str] = []
        self.posted: list[str] = []
        self.entered: list[str] = []

    async def open(self, target: str) -> str:
        self.opened.append(target)
        return self._page(target)

    async def post(self, target: str, data: dict) -> str:
        self.posted.append(target)
        return self._page(target)

    async def enter_course(self, key: str) -> None:
        self.entered.append(key)

    def _page(self, target: str) -> str:
        path = urlsplit(target).path
        if path in self.broken:
            raise EclassError(Failure.NETWORK, "연결 실패")
        return self.pages.get(path, PLAIN_HTML)


# --- 안전 규칙 ---


@pytest.mark.parametrize(
    "path",
    [
        "/ilos/community/notice_write_form.acl",
        "/ilos/mp/report_submit.acl",
        "/ilos/st/course/apply_form.acl",
        "/ilos/lo/filedown.acl",
        "/ilos/main/member/logout.acl",
        "/ilos/etc/board_delete.acl",
        "/ilos/etc/profile_update.acl",
    ],
)
def test_screens_that_change_something_are_never_opened(path):
    assert safe_path(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "/ilos/community/notice_list_form.acl",
        "/ilos/st/schedule/academic_calendar_list_form.acl",
        "/ilos/mp/todo_list_form.acl",
    ],
)
def test_reading_screens_are_allowed(path):
    assert safe_path(path) is True


def test_only_eclass_screens_are_followed():
    assert safe_path("/ilos/images/logo.png") is False
    assert safe_path("") is False


@pytest.mark.parametrize("href", ["#", "javascript:void(0)", "mailto:someone@example.com", ""])
def test_links_that_go_nowhere_are_dropped(href):
    assert normalize(href) is None


def test_query_is_kept_for_opening_but_names_only_for_the_catalog():
    candidate = normalize("/ilos/mp/note_list_form.acl?KJKEY=ABC123&page=2")
    assert candidate.path == "/ilos/mp/note_list_form.acl"
    assert candidate.target.endswith("?KJKEY=ABC123&page=2")
    assert param_names(candidate.query) == ["KJKEY", "page"]


def test_a_course_key_marks_a_per_course_screen():
    assert is_per_course(["KJKEY", "page"]) is True
    assert is_per_course(["page"]) is False


# --- 링크 모으기 ---


def test_menu_links_are_collected_from_href_and_onclick():
    paths = {candidate.path: candidate.name for candidate in collect_links(MAIN_HTML)}

    assert paths["/ilos/community/notice_list_form.acl"] == "공지사항"
    # onclick으로 넘어가는 메뉴도 따라간다
    assert "/ilos/st/schedule/academic_calendar_list_form.acl" in paths
    # 글쓰기·첨부·로그아웃·외부 사이트는 빠진다
    assert not any("write" in path or "filedown" in path or "logout" in path for path in paths)
    assert not any("other.example.com" in path for path in paths)


def test_the_same_screen_is_listed_once():
    html = """
    <a href="/ilos/community/notice_list_form.acl?KJKEY=A">자료구조 공지</a>
    <a href="/ilos/community/notice_list_form.acl?KJKEY=B">운영체제 공지</a>
    """
    assert [candidate.path for candidate in collect_links(html)] == [
        "/ilos/community/notice_list_form.acl"
    ]


# --- 화면 판정 ---


def test_a_list_screen_is_counted_with_its_dates():
    shape = describe(NOTICE_HTML)
    assert shape.listing is True and shape.items == 2 and shape.has_date is True


def test_a_plain_page_is_not_a_list():
    shape = describe(PLAIN_HTML)
    assert shape.listing is False and shape.items == 0


def test_a_deadline_column_is_noticed():
    html = '<table><tr><th>제목</th><th>마감</th></tr><tr><td><a href="?SEQ=1">과제</a></td><td>2026.09.25</td></tr></table>'
    shape = describe(html)
    assert shape.has_due is True and shape.items == 1


def test_sample_files_are_named_after_the_path():
    assert sample_name("/ilos/community/notice_list_form.acl") == "community_notice_list_form"
    assert sample_name("/") == "screen"


# --- 탐색 흐름 ---


async def test_exploring_walks_the_menu_and_saves_samples(tmp_path):
    session = FakeSession(
        {
            "/ilos/main/main_form.acl": MAIN_HTML,
            "/ilos/community/notice_list_form.acl": NOTICE_HTML,
        }
    )
    screens = await explore(session, tmp_path, seeds=("/ilos/main/main_form.acl",), pause=0)

    paths = [screen.path for screen in screens]
    assert "/ilos/main/main_form.acl" in paths
    assert "/ilos/community/notice_list_form.acl" in paths

    notice = next(screen for screen in screens if screen.path.endswith("notice_list_form.acl"))
    assert notice.name == "공지사항" and notice.listing and notice.items == 2
    assert (tmp_path / notice.sample).read_text(encoding="utf-8") == NOTICE_HTML


async def test_exploring_never_opens_a_screen_that_changes_something(tmp_path):
    session = FakeSession({"/ilos/main/main_form.acl": MAIN_HTML})
    await explore(session, tmp_path, seeds=("/ilos/main/main_form.acl",), pause=0)

    assert not any("write" in target or "filedown" in target for target in session.opened)
    assert not any("logout" in target for target in session.opened)


async def test_a_screen_is_opened_once(tmp_path):
    linked = '<a href="/ilos/community/notice_list_form.acl">공지</a>'
    session = FakeSession(
        {
            "/ilos/main/main_form.acl": linked,
            "/ilos/mp/todo_list_form.acl": linked,
            "/ilos/community/notice_list_form.acl": NOTICE_HTML,
        }
    )
    await explore(
        session,
        tmp_path,
        seeds=("/ilos/main/main_form.acl", "/ilos/mp/todo_list_form.acl"),
        pause=0,
    )
    assert session.opened.count("/ilos/community/notice_list_form.acl") == 1


async def test_a_screen_that_will_not_open_is_written_down(tmp_path):
    session = FakeSession(
        {"/ilos/main/main_form.acl": MAIN_HTML}, broken={"/ilos/community/notice_list_form.acl"}
    )
    screens = await explore(session, tmp_path, seeds=("/ilos/main/main_form.acl",), pause=0)

    broken = next(screen for screen in screens if screen.path.endswith("notice_list_form.acl"))
    assert "열리지 않음" in broken.note and broken.sample == ""


async def test_exploring_stops_at_the_given_depth(tmp_path):
    session = FakeSession(
        {
            "/ilos/main/main_form.acl": '<a href="/ilos/a/one_form.acl">하나</a>',
            "/ilos/a/one_form.acl": '<a href="/ilos/a/two_form.acl">둘</a>',
            "/ilos/a/two_form.acl": '<a href="/ilos/a/three_form.acl">셋</a>',
        }
    )
    screens = await explore(
        session, tmp_path, seeds=("/ilos/main/main_form.acl",), max_depth=1, pause=0
    )
    paths = [screen.path for screen in screens]
    assert "/ilos/a/one_form.acl" in paths and "/ilos/a/two_form.acl" not in paths


async def test_exploring_stops_at_the_screen_limit(tmp_path):
    session = FakeSession({"/ilos/main/main_form.acl": MAIN_HTML})
    screens = await explore(
        session, tmp_path, seeds=("/ilos/main/main_form.acl",), max_screens=2, pause=0
    )
    assert len(screens) == 2


# --- 남기는 것 ---


def test_the_catalog_keeps_paths_without_the_school_address(tmp_path):
    path = tmp_path / "eclass_catalog.json"
    write_catalog(
        path,
        [Screen(name="공지사항", path="/ilos/community/notice_list_form.acl", params=["KJKEY"], listing=True, items=2)],
    )
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert saved["screens"][0]["path"] == "/ilos/community/notice_list_form.acl"
    assert saved["screens"][0]["params"] == ["KJKEY"]
    # 도메인도, 질의 값도 남지 않는다
    assert "http" not in path.read_text(encoding="utf-8")


def test_the_summary_lists_the_screens_worth_reading():
    screens = [
        Screen(name="공지사항", path="/ilos/community/notice_list_form.acl", listing=True, items=2, has_date=True),
        Screen(name="안내", path="/ilos/etc/guide_form.acl"),
    ]
    text = summary(screens)
    assert "공지사항" in text and "목록형 1개" in text


# --- 껍데기와 내용 ---

SHELL_HTML = """
<div id="notice_list"></div>
<script>
  function load() {
    $.ajax({url: '/ilos/community/notice_list.acl', type: 'POST'});
  }
</script>
"""


def test_a_shell_screen_points_at_its_content():
    assert data_path(SHELL_HTML, "/ilos/community/notice_list_form.acl") == "/ilos/community/notice_list.acl"


def test_a_plain_screen_has_no_content_address():
    assert data_path(NOTICE_HTML, "/ilos/community/notice_list_form.acl") == ""
    assert data_path(SHELL_HTML, "/ilos/community/notice_view.acl") == ""


def test_a_content_address_that_downloads_is_refused():
    html = "<script>$.ajax({url: '/ilos/mp/file_down.acl'});</script>"
    assert data_path(html, "/ilos/mp/file_down_form.acl") == ""


async def test_exploring_reads_the_content_behind_the_shell(tmp_path):
    session = FakeSession(
        {
            "/ilos/community/notice_list_form.acl": SHELL_HTML,
            "/ilos/community/notice_list.acl": NOTICE_HTML,
        }
    )
    screens = await explore(
        session, tmp_path, seeds=("/ilos/community/notice_list_form.acl",), pause=0
    )

    screen = screens[0]
    assert screen.data_path == "/ilos/community/notice_list.acl"
    assert session.posted == ["/ilos/community/notice_list.acl"]
    # 줄 수와 생김새는 껍데기가 아니라 내용에서 읽는다
    assert screen.listing is True and screen.items == 2
    assert (tmp_path / screen.data_sample).read_text(encoding="utf-8") == NOTICE_HTML


async def test_content_that_will_not_load_is_written_down(tmp_path):
    session = FakeSession(
        {"/ilos/community/notice_list_form.acl": SHELL_HTML},
        broken={"/ilos/community/notice_list.acl"},
    )
    screens = await explore(
        session, tmp_path, seeds=("/ilos/community/notice_list_form.acl",), pause=0
    )
    assert "내용 주소를 받지 못함" in screens[0].note and screens[0].data_sample == ""


@pytest.mark.parametrize(
    "path",
    ["/ilos/mp/file_down.acl", "/ilos/lo/filedown.acl", "/ilos/co/down_load.acl"],
)
def test_downloads_are_blocked_in_every_spelling(path):
    assert safe_path(path) is False


def test_a_no_data_row_is_not_counted():
    """'조회할 자료가 없습니다'는 칸 하나를 늘려 쓰므로 줄로 세지 않는다."""
    html = """
    <table>
      <tr><th>번호</th><th>제목</th><th>공개일</th></tr>
      <tr><td colspan="3">조회할 자료가 없습니다</td></tr>
    </table>
    """
    shape = describe(html)
    assert shape.listing is False and shape.items == 0


def test_rows_are_counted_without_an_article_number():
    """줄마다 글번호가 붙지 않는 화면도 있다 (쪽지함·과제 목록)."""
    html = """
    <table>
      <tr><th>보낸사람</th><th>제목</th><th>날짜</th></tr>
      <tr><td>홍길동</td><td onclick="viewMsg(1)">안내드립니다</td><td>2026.09.19</td></tr>
      <tr><td>김철수</td><td onclick="viewMsg(2)">확인 바랍니다</td><td>2026.09.20</td></tr>
    </table>
    """
    shape = describe(html)
    assert shape.listing is True and shape.items == 2 and shape.has_date is True


async def test_course_room_screens_wait_for_the_room(tmp_path):
    """과목방 안에서만 뜻이 있는 화면은 방 밖에서 열지 않는다."""
    session = FakeSession(
        {"/ilos/main/main_form.acl": '<a href="/ilos/st/course/report_list.acl">과제</a>'}
    )
    screens = await explore(
        session, tmp_path, seeds=("/ilos/main/main_form.acl",), pause=0
    )
    assert not any("report_list" in target for target in session.opened)
    assert [screen.path for screen in screens] == ["/ilos/main/main_form.acl"]
