"""과제: 제출 여부를 읽어 할 일을 닫고, 과제 상세(설명·첨부 파일 이름)를 한 번 읽어 둔다."""

from app.collectors.eclass.parse import parse_assignment, parse_list, todo_item_id
from app.collectors.eclass.sources.board import (
    NOT_SUBMITTED_TEXT,
    SUBMITTED_TEXT,
    BoardSource,
    strip_status,
    with_status,
)
from app.core.config import Level
from app.core.events import Event, EventKind
from app.scheduler.ingest import Ingestor
from app.storage.eclass import EclassRepository
from tests.collectors.test_eclass_board import COURSES, FakeSession
from tests.conftest import kst, make_event

NOW = kst(9, 20, 9)
REPORT_PATH = "/ilos/st/course/report_list.acl"
VIEW_PATH = "/ilos/st/course/report_view_form.acl"

# 실제 과제 게시판과 같은 모양. 제출 표시는 글자가 아니라 그림의 alt에 있다.
REPORT_HTML = """
<table class="bbslist">
  <tr><th>번호</th><th>중요</th><th>제목</th><th>진행</th><th>제출</th><th>점수</th><th>배점</th><th>마감일</th></tr>
  <tr>
    <td>2</td><td></td>
    <td class="left" onclick="pageMove('/ilos/st/course/report_view_form.acl?RT_SEQ=111&amp;SCH_KEY=');">
      <a class="site-link"><div class="subjt_top">3장 연습문제</div></a></td>
    <td>진행중</td><td><img alt="미제출" src="x.png"></td><td>비공개</td><td>10</td>
    <td>2026.09.22 오후 11:59</td>
  </tr>
  <tr>
    <td>1</td><td></td>
    <td class="left" onclick="pageMove('/ilos/st/course/report_view_form.acl?RT_SEQ=222&amp;SCH_KEY=');">
      <a class="site-link"><div class="subjt_top">2장 연습문제</div></a></td>
    <td>진행중</td><td><img alt="제출" src="y.png"></td><td>비공개</td><td>10</td>
    <td>2026.09.19 오후 11:59</td>
  </tr>
</table>
"""

VIEW_HTML = """
<table class="bbsview">
  <tr><th>제목</th><td colspan="3">3장 연습문제</td></tr>
  <tr><th>제출방식</th><td>온라인</td><th>게시일</th><td>2026.09.16 오후 6:00</td></tr>
  <tr><th>마감일</th><td>2026.09.22 오후 11:59</td><th>배점</th><td>10</td></tr>
  <tr><th>지각제출</th><td>불허</td><th>점수공개</th><td>미공개</td></tr>
</table>
<div class="textviewer">첨부된 파일의 문제를 풀어 기한 안에 올리세요. 첨부파일(1개) - 3장 문제.docx (20.1KB)</div>
<a href="/ilos/co/efile_download.acl?FILE_SEQ=1">- 3장 문제.docx (20.1KB)</a>
"""


def report_source(items=None) -> BoardSource:
    return BoardSource(
        key="course_report_list",
        label="과제",
        path=REPORT_PATH,
        level=Level.STORE,
        per_course=True,
        items=items,
        clock=lambda: NOW,
        submission_category="과제",
    )


# --- 파서 ---


def test_submission_is_read_by_exact_match():
    """'미제출'에도 '제출'이 들어 있다. 포함 여부로 보면 전부 제출로 읽힌다."""
    rows = parse_list(REPORT_HTML, NOW).rows
    assert [(row.article_id, row.submitted) for row in rows] == [("111", False), ("222", True)]


def test_boards_without_a_submission_column_know_nothing():
    html = REPORT_HTML.replace("<th>제출</th>", "<th>첨부</th>")
    assert all(row.submitted is None for row in parse_list(html, NOW).rows)


def test_assignment_detail_keeps_file_names_only():
    detail = parse_assignment(VIEW_HTML)
    assert detail.description == "첨부된 파일의 문제를 풀어 기한 안에 올리세요."
    assert detail.attachments == ["3장 문제.docx (20.1KB)"]
    assert (detail.method, detail.late) == ("온라인", "불허")
    summary = detail.summary()
    assert "첨부: 3장 문제.docx (20.1KB)" in summary and "지각제출 불허" in summary
    # 내려받는 주소는 글에 남기지 않는다
    assert "efile_download" not in summary


def test_status_line_comes_and_goes():
    body = with_status("설명", False)
    assert body.endswith(NOT_SUBMITTED_TEXT)
    assert strip_status(body) == "설명"
    # 예전 형식(상태 한 마디만 저장)도 상세 없음으로 본다
    assert strip_status(SUBMITTED_TEXT) == ""
    assert with_status("", None) == ""


# --- 소스 ---


async def test_submitted_assignments_close_their_todo(db):
    session = FakeSession({REPORT_PATH: REPORT_HTML, VIEW_PATH: VIEW_HTML})
    result = await report_source(EclassRepository(db)).fetch(session, COURSES[:1])

    assert [event.kind for event in result.events] == [EventKind.SUBMITTED]
    event = result.events[0]
    # 할 일의 이름과 게시판 글번호가 같은 번호로 이어진다
    assert event.meta["todo_ref"] == todo_item_id("과제", "KJ1", "222")


async def test_new_assignments_are_read_once_with_their_status(db):
    items = EclassRepository(db)
    session = FakeSession({REPORT_PATH: REPORT_HTML, VIEW_PATH: VIEW_HTML})
    result = await report_source(items).fetch(session, COURSES[:1])

    first = result.items[0]
    assert "3장 문제.docx" in first.body and first.body.endswith("제출 상태: " + NOT_SUBMITTED_TEXT)
    for item in result.items:
        await items.upsert(item, NOW)
    opened = [path for path in session.opened if path.startswith(VIEW_PATH)]
    assert len(opened) == 2

    # 다음 차례: 상세는 다시 열지 않고, 바뀐 제출 상태만 새로 붙인다
    again = FakeSession({REPORT_PATH: REPORT_HTML.replace('alt="미제출"', 'alt="제출"'), VIEW_PATH: VIEW_HTML})
    second = await report_source(items).fetch(again, COURSES[:1])
    assert not [path for path in again.opened if path.startswith(VIEW_PATH)]
    assert second.items[0].body.endswith("제출 상태: " + SUBMITTED_TEXT)
    assert "3장 문제.docx" in second.items[0].body


async def test_a_detail_that_fails_to_open_is_tried_next_time(db):
    items = EclassRepository(db)
    session = FakeSession({REPORT_PATH: REPORT_HTML}, broken={VIEW_PATH})
    result = await report_source(items).fetch(session, COURSES[:1])

    assert result.items[0].body == "제출 상태: " + NOT_SUBMITTED_TEXT
    await items.upsert(result.items[0], NOW)
    retry = FakeSession({REPORT_PATH: REPORT_HTML, VIEW_PATH: VIEW_HTML})
    await report_source(items).fetch(retry, COURSES[:1])
    assert any(path.startswith(VIEW_PATH) for path in retry.opened)


# --- 할 일 닫기 ---


def submitted(todo_ref: str) -> Event:
    return Event(
        source="eclass",
        kind=EventKind.SUBMITTED,
        title="과제",
        ref_id=f"{todo_ref}:submitted",
        meta={"todo_ref": todo_ref},
    )


async def test_the_ingestor_closes_the_todo_quietly(todos, dispatcher, notifier):
    ref = todo_item_id("과제", "KJ1", "222")
    await todos.add_from_event(make_event(ref, kind=EventKind.DEADLINE, due_at=kst(9, 22, 23, 59)), NOW)
    ingestor = Ingestor(todos, dispatcher)

    decisions = await ingestor.ingest([submitted(ref)], NOW)

    assert decisions == [] and notifier.sent == []
    assert (await todos.get_by_ref(ref)).status == "done"
    # 다시 와도, 할 일이 없어도 그냥 넘어간다
    assert await ingestor.ingest([submitted(ref), submitted("eclass:과제:없음:0")], NOW) == []
