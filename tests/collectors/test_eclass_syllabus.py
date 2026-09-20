"""강의계획서 소스 확인. 게시판이 아니라 표로 된 문서라 따로 읽는다."""

from datetime import timedelta

from app.collectors.eclass.parse import CourseRow, parse_syllabus
from app.collectors.eclass.session import EclassError, Failure
from app.collectors.eclass.sources.syllabus import SyllabusSource
from app.storage.eclass import EclassItem, ItemChange
from tests.conftest import kst

NOW = kst(9, 20, 9)

PLAN_HTML = """
<div id="content_text">
  <table class="bbsview">
    <tr><th>교과목명</th><td>자료구조</td><th>이수구분</th><td>전공필수</td></tr>
    <tr><th>교수</th><td>홍길동</td><th>학점/강의</th><td>3/3</td></tr>
    <tr><th>강의시간</th><td>월 [3~4] 11:00~12:50</td><th>E-mail</th><td>prof@example.ac.kr</td></tr>
    <tr><th>학습 평가방법</th><td>중간(30%), 기말(40%), 과제(30%)</td></tr>
  </table>
  <table class="bbsview">
    <tr><th>주차</th><th>강의범위 및 내용</th></tr>
    <tr><td>제 1주</td><td>오리엔테이션</td></tr>
  </table>
</div>
"""

EMPTY_PLAN = '<div id="content_text"><p>등록된 강의계획서가 없습니다.</p></div>'

COURSES = [CourseRow(kjkey="KJ1", name="자료구조"), CourseRow(kjkey="KJ2", name="운영체제")]


class FakeSession:
    def __init__(self, pages: dict[str, str], broken: set[str] | None = None) -> None:
        self.pages = pages
        self.broken = broken or set()
        self.entered: list[str] = []

    async def enter_course(self, key: str) -> None:
        if key in self.broken:
            raise EclassError(Failure.NETWORK, "연결 실패")
        self.entered.append(key)

    async def open(self, path: str) -> str:
        return self.pages.get(self.entered[-1] if self.entered else "", EMPTY_PLAN)


# --- 파서 ---


def test_the_information_table_is_read_as_pairs():
    pairs = dict(parse_syllabus(PLAN_HTML))
    assert pairs["교과목명"] == "자료구조" and pairs["이수구분"] == "전공필수"
    assert pairs["학습 평가방법"] == "중간(30%), 기말(40%), 과제(30%)"
    assert pairs["제 1주"] == "오리엔테이션"


def test_a_page_without_the_table_gives_nothing():
    assert parse_syllabus(EMPTY_PLAN) == []


# --- 소스 ---


async def test_each_course_syllabus_becomes_one_item():
    session = FakeSession({"KJ1": PLAN_HTML, "KJ2": PLAN_HTML})
    result = await SyllabusSource().fetch(session, COURSES)

    assert len(result.items) == 2 and session.entered == ["KJ1", "KJ2"]
    first = result.items[0]
    assert first.item_id == "eclass:course_plan:KJ1"
    assert first.title == "자료구조 강의계획서" and first.kind == "강의계획서"
    assert "학습 평가방법: 중간(30%)" in first.body and "강의시간: 월" in first.body


async def test_a_course_without_a_syllabus_is_skipped():
    session = FakeSession({"KJ1": PLAN_HTML})
    result = await SyllabusSource().fetch(session, COURSES)
    assert len(result.items) == 1 and result.suspicious is False


async def test_every_course_missing_a_syllabus_looks_like_a_changed_page():
    session = FakeSession({})
    result = await SyllabusSource().fetch(session, COURSES)
    assert result.items == [] and result.suspicious is True


async def test_a_locked_course_does_not_stop_the_rest():
    session = FakeSession({"KJ2": PLAN_HTML}, broken={"KJ1"})
    result = await SyllabusSource().fetch(session, COURSES)
    assert [item.course for item in result.items] == ["운영체제"]


async def test_a_syllabus_is_never_announced():
    """학기에 한 번 바뀌는 문서다. 알릴 일이 아니라 물어보면 답할 것이다."""
    item = EclassItem(item_id="eclass:course_plan:KJ1", kind="강의계획서", title="자료구조 강의계획서")
    assert SyllabusSource().event_for(item, ItemChange.NEW, NOW) is None


def test_a_syllabus_is_read_once_a_day():
    assert SyllabusSource().interval == timedelta(hours=24)
