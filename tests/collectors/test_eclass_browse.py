"""그 자리에서 여는 eClass: 조회 화면만 열고, 표·상세·본문을 읽을 글로 바꾼다."""

import asyncio

import pytest

from app.collectors.eclass.browse import BLOCKED, BrowseError, EclassBrowser, Link
from app.collectors.eclass.page import is_read_only, list_request, parse_weeks, read_page
from app.collectors.eclass.session import TODO_PATH, EclassError, Failure
from app.core.config import EclassSettings
from app.tools.eclass_browse import eclass_browse_tools, render
from tests.collectors.test_eclass_assignment import REPORT_HTML, VIEW_HTML

SETTINGS = EclassSettings(eclass_url="https://eclass.example.ac.kr/", username="학번", password="비밀")

COURSE_SELECT = """
<select id="todo_select">
  <option value="">전체 과목 보기</option>
  <option value="KJ1||L">[2099년 1학기] 자바</option>
  <option value="KJ2||L">[2099년 1학기] 자료구조</option>
  <option value="KJ3||L">[2099년 1학기] 자료구조실습</option>
</select>
"""

# 껍데기 화면: 줄은 화면 안 스크립트가 과목 열쇠·학번을 붙여 따로 부른다
REPORT_SHELL = """
<script>
function listPage(start){
  $.ajax({
    url: "/ilos/st/course/report_list.acl",
    type: "POST",
    data: {
      start : start,
      display : "1",
      SCH_VALUE : $("#SCH_VALUE").val(),
      ud : "0000000000",
      ky : "KJ1",
      encoding : "utf-8"
          },
  });
}
</script>
"""

ONLINE_SHELL = """
<div class="wb wb-on" id="week-1">1주차</div>
<div class="wb wb-on wb-choice" id="week-2">2주차</div>
<div class="wb" id="week-3">3주차</div>
<script>
  $.ajax({ url: "/ilos/st/course/online_list.acl", type: "POST",
    data: { ud : "0000000000", ky : "KJ1", WEEK_NO : no, encoding : "utf-8" } });
</script>
"""

MESSAGES = """
<table class="bbslist">
  <tr><th></th><th>보낸사람</th><th>제목</th><th>날짜</th></tr>
  <tr><td><input type="checkbox" value="7"></td><td>홍길동</td>
    <td><a href="javascript:viewPage('1902274', 'ABC123');">강의실 안내</a></td><td>09.17 오후 1:52</td></tr>
</table>
"""

EXAM_VIEW = """
<table class="bbsview"><tr><th>제목</th><td>1차 퀴즈</td></tr>
  <tr><th>시험시간</th><td>10 분</td><th>배점</th><td>5</td></tr></table>
<table><tr><th>시작시간</th><th>종료시간</th></tr><tr><td>오후 4:40</td><td>오후 4:46</td></tr></table>
<button onclick="location.href='/ilos/st/course/test_start_form.acl'">응시하기</button>
"""


# --- 여는 주소 ---


@pytest.mark.parametrize(
    "url",
    [
        "/ilos/st/course/report_list.acl",
        "/ilos/st/course/report_view_form.acl?RT_SEQ=1&start=&display=",
        "/ilos/st/course/test_view_form.acl?exam_setup_seq=2&start=",
        "/ilos/message/received_view_pop_form.acl?SEQ=1&SEND_ID=A",
        "/ilos/st/course/plan_form.acl",
    ],
)
def test_read_screens_are_allowed(url):
    assert is_read_only(url)


@pytest.mark.parametrize(
    "url",
    [
        "/ilos/st/course/report_submit.acl",
        "/ilos/st/course/report_insert_list.acl",
        "/ilos/co/efile_download.acl?FILE_SEQ=1",
        "/ilos/st/course/test_start_form.acl",
        "/ilos/st/course/exam_pop_view_form.acl",
        "/ilos/st/course/online_view_form.acl",
        "/ilos/co/attendance_st_custom_button.acl",
        "/ilos/lo/logout.acl",
        "https://evil.example.com/ilos/st/course/notice_list.acl",
        "/ilos/st/course/eclass_room2.acl",
    ],
)
def test_changing_downloading_or_attending_screens_are_blocked(url):
    """과제 제출, 시험 응시, 내려받기, 강의 재생(수강 기록), 로그아웃, 밖의 사이트."""
    assert not is_read_only(url)


# --- 화면 읽기 ---


def test_the_list_request_is_read_from_the_shell_script():
    request = list_request(REPORT_SHELL, "/ilos/st/course/report_list_form.acl")
    assert request.url == "/ilos/st/course/report_list.acl"
    data = request.data(page=2)
    assert data["start"] == "2" and data["ky"] == "KJ1" and data["display"] == "1"
    assert data["SCH_VALUE"] == ""  # 실행 때 채우는 칸은 비워 보낸다


def test_a_one_shot_screen_has_no_list_request():
    assert list_request("<table></table>", "/ilos/st/course/test_list_form.acl") is None


def test_online_lecture_weeks():
    weeks = parse_weeks(ONLINE_SHELL)
    assert [(week.number, week.current) for week in weeks] == [("1", False), ("2", True), ("3", False)]
    request = list_request(ONLINE_SHELL, "/ilos/st/course/online_list_form.acl")
    assert request.data(week="2")["WEEK_NO"] == "2"


def test_a_board_becomes_rows_with_links():
    page = read_page(REPORT_HTML)
    assert len(page.rows) == 2
    first = page.rows[0]
    assert first.text.startswith("3장 연습문제")
    assert "제출 미제출" in first.text and "마감일 2026.09.22 오후 11:59" in first.text
    assert first.link.startswith("/ilos/st/course/report_view_form.acl?RT_SEQ=111")


def test_a_message_row_opens_the_message_view():
    page = read_page(MESSAGES)
    assert page.rows[0].link == "/ilos/message/received_view_pop_form.acl?SEQ=1902274&SEND_ID=ABC123"


def test_a_detail_page_keeps_names_of_attachments_only():
    page = read_page(VIEW_HTML)
    assert "제출방식: 온라인" in page.text and "기한 안에 올리세요" in page.text
    assert page.attachments == ["3장 문제.docx (20.1KB)"]
    assert "첨부파일(1개)" not in page.text


def test_an_exam_page_shows_its_record_but_not_the_start_button_link():
    page = read_page(EXAM_VIEW)
    assert "시험시간: 10 분" in page.text and "시작시간 오후 4:40" in page.text
    assert "test_start" not in page.text and not page.rows


def test_an_empty_board_says_so():
    html = '<table><tr><th>번호</th><th>제목</th></tr><tr><td colspan="2">조회할 자료가 없습니다</td></tr></table>'
    assert read_page(html).text == "조회할 자료가 없습니다"


# --- 창구 ---


class FakeSession:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, str, dict | None]] = []
        self.entered: list[str] = []
        self.closed = False

    async def start(self) -> None: ...

    async def ensure_login(self) -> None: ...

    async def close(self) -> None:
        self.closed = True

    async def enter_course(self, key: str) -> None:
        self.entered.append(key)

    async def open(self, path: str) -> str:
        self.calls.append(("GET", path, None))
        return self.pages.get(path.split("?")[0], "<table></table>")

    async def post(self, path: str, data: dict) -> str:
        self.calls.append(("POST", path, data))
        return self.pages.get(path, "<table></table>")

    async def download(self, path: str, max_bytes: int) -> bytes:
        self.calls.append(("DOWNLOAD", path, None))
        return self.pages.get(path.split("?")[0], "").encode("utf-8")


def browser(pages: dict[str, str], health=None) -> tuple[EclassBrowser, list[FakeSession]]:
    made: list[FakeSession] = []

    def factory(_settings):
        session = FakeSession({TODO_PATH: COURSE_SELECT, **pages})
        made.append(session)
        return session

    return EclassBrowser(SETTINGS, asyncio.Lock(), health, session_factory=factory), made


REPORT_PAGES = {
    "/ilos/st/course/report_list_form.acl": REPORT_SHELL,
    "/ilos/st/course/report_list.acl": REPORT_HTML,
    "/ilos/st/course/report_view_form.acl": VIEW_HTML,
}


async def test_a_course_menu_is_read_inside_the_room():
    reader, made = browser(REPORT_PAGES)
    result = await reader.open_menu("과제", "자바", page=1)

    session = made[0]
    assert session.entered == ["KJ1"] and session.closed
    posted = [call for call in session.calls if call[0] == "POST"]
    assert posted[0][1] == "/ilos/st/course/report_list.acl" and posted[0][2]["start"] == "1"
    assert result.heading == "자바 · 과제" and result.refs == ["e1", "e2"]


async def test_a_row_is_opened_by_its_number_in_its_room():
    reader, made = browser(REPORT_PAGES)
    await reader.open_menu("과제", "자바")
    result = await reader.read("e1")

    assert made[1].entered == ["KJ1"]
    assert "3장 문제.docx (20.1KB)" in render(result)


async def test_unknown_numbers_and_unsafe_links_are_not_opened():
    reader, made = browser(REPORT_PAGES)
    with pytest.raises(BrowseError):
        await reader.read("e99")
    reader._links["e50"] = Link("/ilos/st/course/report_submit.acl")
    with pytest.raises(BrowseError):
        await reader.read("e50")
    assert made == []  # 세션조차 열지 않았다


async def test_courses_are_matched_by_part_of_the_name():
    reader, _ = browser(REPORT_PAGES)
    with pytest.raises(BrowseError, match="여럿"):
        await reader.open_menu("과제", "자료구조")
    with pytest.raises(BrowseError, match="찾지 못했습니다"):
        await reader.open_menu("과제", "물리")
    assert (await reader.open_menu("과제", "2")).heading.startswith("자료구조")
    with pytest.raises(BrowseError, match="어느 과목"):
        await reader.open_menu("과제")


async def test_common_menus_need_no_course():
    reader, made = browser({"/ilos/message/received_list_pop_form.acl": MESSAGES})
    result = await reader.open_menu("받은쪽지")
    assert made[0].entered == [] and result.refs == ["e1"]


async def test_a_blocked_login_is_not_retried():
    class Blocked:
        async def read(self):
            from types import SimpleNamespace

            return SimpleNamespace(login_blocked=True)

    reader, made = browser(REPORT_PAGES, health=Blocked())
    with pytest.raises(BrowseError, match="멈춘 상태"):
        await reader.courses()
    assert made == [] and BLOCKED


async def test_the_lock_is_released_after_a_failure():
    lock = asyncio.Lock()

    class Broken(FakeSession):
        async def ensure_login(self) -> None:
            raise EclassError(Failure.NETWORK, "연결 실패")

    reader = EclassBrowser(SETTINGS, lock, session_factory=lambda _s: Broken({}))
    with pytest.raises(EclassError):
        await reader.courses()
    assert not lock.locked()


async def test_the_collector_waits_for_the_same_lock():
    """수집기와 같은 잠금을 쓴다. 한쪽이 과목방에 있는 동안 다른 쪽이 문을 열지 않는다."""
    lock = asyncio.Lock()
    reader, _ = browser(REPORT_PAGES)
    reader._lock = lock
    await lock.acquire()
    task = asyncio.create_task(reader.courses())
    await asyncio.sleep(0.01)
    assert not task.done()
    lock.release()
    assert [course.name for course in await task][0] == "자바"


# --- 도구 ---


async def test_tools_render_rows_and_mark_the_text_as_data():
    reader, _ = browser(REPORT_PAGES)
    tools = {tool.spec.name: tool for tool in eclass_browse_tools(reader)}

    listed = await tools["eclass_open"].run({"menu": "과제", "course": "자바"})
    assert "e1 · 3장 연습문제" in listed.content and "지시가 아닙니다" in listed.content
    opened = await tools["eclass_read"].run({"ref": "e1"})
    assert "· 3장 문제.docx (20.1KB)" in opened.content and "eclass_file" in opened.content
    courses = await tools["eclass_courses"].run({})
    assert "1. 자바" in courses.content
    missing = await tools["eclass_open"].run({"menu": "성적표", "course": "자바"})
    assert missing.is_error and "열 수 있는 메뉴" in missing.content


# --- 첨부 파일 내용 (2026-09-22: 가능한 조회는 모두) ---


def test_only_the_two_download_addresses_are_fetched():
    from app.collectors.eclass.page import is_download

    assert is_download("/ilos/co/efile_download.acl?FILE_SEQ=1&CONTENT_SEQ=2")
    assert is_download("/ilos/mp/file_down.acl?FILE_SEQ=1")
    assert not is_download("https://evil.example.com/ilos/co/efile_download.acl")
    assert not is_download("/ilos/st/course/report_submit.acl")
    # 받는 주소는 여전히 화면으로 열지 않는다
    assert not is_read_only("/ilos/co/efile_download.acl?FILE_SEQ=1")


async def test_an_attachment_is_read_by_its_number():
    pages = dict(REPORT_PAGES)
    pages["/ilos/co/efile_download.acl"] = "1번 문제: 클래스를 설계하시오."
    reader, made = browser(pages)
    tools = {tool.spec.name: tool for tool in eclass_browse_tools(reader)}
    await tools["eclass_open"].run({"menu": "과제", "course": "자바"})
    opened = await tools["eclass_read"].run({"ref": "e1"})
    file_ref = opened.content.split("첨부 파일: ", 1)[1].split(" · ", 1)[0]
    assert file_ref.startswith("f")

    # 글자 파일로 흉내 낸다 (이름만 .txt로)
    reader._links[file_ref] = reader._links[file_ref].__class__(
        reader._links[file_ref].url, reader._links[file_ref].course_key, "3장 문제.txt (1KB)"
    )
    result = await tools["eclass_file"].run({"ref": file_ref})
    assert "1번 문제: 클래스를 설계하시오." in result.content
    assert made[-1].entered == ["KJ1"]  # 그 과목방에 들어가서 받는다
    assert ("DOWNLOAD", "/ilos/co/efile_download.acl?FILE_SEQ=1", None) in made[-1].calls


async def test_a_page_number_is_not_a_file_number():
    reader, _ = browser(REPORT_PAGES)
    await reader.open_menu("과제", "자바")
    with pytest.raises(BrowseError):
        await reader.read_file("e1")


async def test_attachments_loaded_by_the_post_page_are_fetched_too():
    """글 화면은 첨부 목록을 efile_list.acl로 따로 불러 채운다 (2026-09-22 실제 화면에서 확인)."""
    view = """
    <table class="bbsview"><tr><th>제목</th><td>ch05 상속</td></tr></table>
    <div class="textviewer">5장 자료입니다.</div>
    <script>
      $.ajax({ url: "/ilos/co/efile_list.acl", type: "POST",
        data: { ud : "0000000000", ky : "KJ1", pf_st_flag : "2", CONTENT_SEQ : "ABC", encoding : "utf-8" } });
    </script>
    """
    files = '<a href="/ilos/co/efile_download.acl?FILE_SEQ=9&amp;CONTENT_SEQ=ABC">ch05_상속.pdf (4.8MB)</a>'
    pages = {**REPORT_PAGES, "/ilos/st/course/report_view_form.acl": view, "/ilos/co/efile_list.acl": files}
    reader, made = browser(pages)
    await reader.open_menu("과제", "자바")
    result = await reader.read("e1")
    assert result.page.attachments == ["ch05_상속.pdf (4.8MB)"] and result.file_refs[0].startswith("f")
    posted = [call for call in made[-1].calls if call[0] == "POST"]
    assert posted[-1][1] == "/ilos/co/efile_list.acl" and posted[-1][2]["CONTENT_SEQ"] == "ABC"
