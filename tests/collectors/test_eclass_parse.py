import pytest

from app.collectors.eclass.parse import (
    parse_courses,
    parse_datetime,
    parse_notices,
    parse_todo_list,
)
from app.collectors.eclass.session import Failure, classify_login, logged_in, logged_out
from tests.conftest import kst

TODO_HTML = """
<div class="todo_list">
  <div class="todo_wrap" onclick="goLecture('KJ1','7','report')">
    <input type="hidden" id="gubun_1" value="report">
    <input type="hidden" id="kj_1" value="KJ1">
    <div class="todo_subjt">자료구조</div>
    <div class="todo_title">과제 2 제출</div>
    <div class="todo_d_day">D-3</div>
    <div class="todo_date">2026.09.14 오전 09:00</div>
    <div class="todo_date">2026.09.25 오후 11:59</div>
  </div>
  <div class="todo_wrap" onclick="goLecture('KJ2','3','lecture_weeks')">
    <div class="todo_subjt">운영체제</div>
    <div class="todo_title">3주차 온라인 강의</div>
    <div class="todo_date">2026.09.22 오후 12:00</div>
  </div>
  <div class="todo_wrap">
    <div class="todo_title">깨진 줄</div>
  </div>
</div>
"""

COURSE_HTML = """
<div class="content-container" onclick="eclassRoom('KJ1')">
  <div class="content-title">자료구조</div>
  <ul class="content-author"><li>홍길동 교수</li><li>월 3-4교시</li></ul>
</div>
<div class="content-container">
  <div class="content-title">키 없는 과목</div>
</div>
"""

NOTICE_HTML = """
<table>
  <tr><th>제목</th><th>날짜</th></tr>
  <tr><td><a href="/ilos/community/notice_view_form.acl?ARTL_NUM=123">휴강 안내</a></td><td>2026.09.19</td></tr>
  <tr><td><a href="/ilos/community/notice_view_form.acl?ARTL_NUM=124">시험 일정 변경</a></td><td>2026.09.20</td></tr>
</table>
"""


# --- 날짜 ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026.09.25 오후 11:59", kst(9, 25, 23, 59)),
        ("2026.09.25 오전 09:00", kst(9, 25, 9, 0)),
        ("2026.09.25 오후 12:30", kst(9, 25, 12, 30)),
        ("2026.09.25 오전 12:10", kst(9, 25, 0, 10)),
        ("2026-09-25", kst(9, 25, 0)),
        ("2026.09.14 오전 09:00 ~ 2026.09.25 오후 11:59", kst(9, 25, 23, 59)),
    ],
)
def test_datetime_is_read_as_seoul_time(text, expected):
    assert parse_datetime(text) == expected


@pytest.mark.parametrize("text", ["", None, "마감 없음", "2026.13.45"])
def test_unreadable_dates_are_none(text):
    assert parse_datetime(text) is None


# --- 할 일 목록 ---


def test_todo_rows_carry_course_category_and_due():
    result = parse_todo_list(TODO_HTML)
    assert result.skipped == 1 and not result.suspicious

    first, second = result.rows
    assert (first.course, first.title, first.category) == ("자료구조", "과제 2 제출", "과제")
    assert first.due_at == kst(9, 25, 23, 59)
    assert first.item_id == "eclass:과제:KJ1:7"
    assert (second.category, second.due_at) == ("온라인 강의", kst(9, 22, 12))


def test_all_rows_unreadable_is_suspicious():
    result = parse_todo_list('<div class="todo_wrap"><div class="todo_title">제목뿐</div></div>')
    assert result.rows == [] and result.skipped == 1 and result.suspicious is True


def test_empty_page_is_not_suspicious():
    result = parse_todo_list("<html><body>할 일이 없습니다</body></html>")
    assert result.rows == [] and result.suspicious is False


# --- 과목·공지 ---


def test_courses_need_a_room_key():
    result = parse_courses(COURSE_HTML)
    assert [row.name for row in result.rows] == ["자료구조"]
    assert result.rows[0].kjkey == "KJ1" and result.rows[0].professor == "홍길동 교수"
    assert result.rows[0].time == "월 3-4교시" and result.skipped == 1


def test_notices_are_read_with_article_ids():
    result = parse_notices(NOTICE_HTML, course="자료구조")
    assert [row.article_id for row in result.rows] == ["123", "124"]
    assert result.rows[0].title == "휴강 안내"
    assert result.rows[0].item_id == "eclass:notice:자료구조:123"
    assert result.rows[0].posted_on == kst(9, 19, 0)


# --- 로그인 결과 판정 ---


MAIN_URL = "https://eclass.example.ac.kr/ilos/main/main_form.acl"
LOGIN_URL = "https://eclass.example.ac.kr/ilos/main/member/login_form.acl"
LOGGED_IN_HTML = '<a href="/ilos/lo/logout.acl">로그아웃</a>'
LOGIN_FORM_HTML = '<input id="usr_id"><input id="usr_pwd">'


@pytest.mark.parametrize(
    ("url", "html", "expected"),
    [
        # 로그인에 실패해도 메인 주소로 보내 주므로, 주소만으로는 판단하지 않는다
        (MAIN_URL, LOGGED_IN_HTML, None),
        (MAIN_URL, LOGIN_FORM_HTML, Failure.LOGIN),
        (LOGIN_URL, LOGIN_FORM_HTML, Failure.LOGIN),
        (MAIN_URL, "<div class='g-recaptcha'>", Failure.CAPTCHA),
        (LOGIN_URL, "자동입력 방지", Failure.CAPTCHA),
        ("https://eclass.example.ac.kr/ilos/etc/unknown.acl", "<html>?</html>", Failure.LAYOUT),
    ],
)
def test_login_result_is_classified(url, html, expected):
    assert classify_login(url, html) is expected


def test_logged_in_needs_a_real_marker():
    assert logged_in(LOGGED_IN_HTML) is True
    # 스크립트·CSS 이름에 logout이 들어간 것만으로는 로그인으로 보지 않는다
    assert logged_in('<div class="header_logout"></div><script src="session_check.js"></script>') is False


def test_logged_out_is_detected_without_the_marker():
    assert logged_out(LOGIN_URL, LOGIN_FORM_HTML) is True
    assert logged_out(MAIN_URL, LOGGED_IN_HTML) is False
