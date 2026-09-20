"""eClass HTML 파싱 (순수 함수).

브라우저 계층과 분리해 두어, 저장해 둔 HTML 조각만으로 로그인 없이 시험할 수 있다 (docs/refs/eclass-cli.md).
- 날짜는 Asia/Seoul로 해석해 UTC로 저장한다. 실행 머신 시간대에 기대지 않는다.
- 필수 항목이 비면 그 줄만 건너뛰고, 몇 줄을 못 읽었는지 세어 둔다. 전부 못 읽으면 호출한 쪽이 '구조 변경'으로 보고한다.
- 여기서 다루는 글은 외부에서 온 데이터다. 지시로 해석하지 않는다 (절대 규칙 8).
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from app.core.clock import KST

# 할 일 유형(gubun) → 우리 분류
CATEGORIES = {
    "report": "과제",
    "test": "시험",
    "quiz": "시험",
    "project": "프로젝트",
    "lecture_weeks": "온라인 강의",
}
DEFAULT_CATEGORY = "온라인 강의"

_DATE = re.compile(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})(?:\s*(오전|오후)?\s*(\d{1,2}):(\d{2}))?")
# 연도 없이 "09.17 오후 1:52"로 적는 화면이 있다 (쪽지함). 연도는 기준 시각에서 채운다.
_SHORT_DATE = re.compile(r"(\d{1,2})[.\-/](\d{1,2})(?:\s*(오전|오후)?\s*(\d{1,2}):(\d{2}))?")
_GO_LECTURE = re.compile(r"goLecture\(\s*['\"]?([^'\",)]+)['\"]?\s*,\s*['\"]?([^'\",)]+)['\"]?\s*,\s*['\"]?([^'\",)]*)")
_ECLASS_ROOM = re.compile(r"eclassRoom\(\s*['\"]?([^'\",)]+)")
_ARTL_NUM = re.compile(r"ARTL_NUM=(\d+)")
# 줄 하나를 가리키는 번호. 화면마다 이름이 다르다 (공지 ARTL_NUM, 과제 RT_SEQ, 쪽지 checkbox).
_ROW_ID = re.compile(r"(?:ARTL_NUM|RT_SEQ|SEQ|IDX)=(\d+)", re.IGNORECASE)
_VIEW_PAGE = re.compile(r"viewPage\(\s*['\"](\d+)")
_IMPT_SEQ = re.compile(r'impt_seq="(\d+)"')
# 목록 줄에서 글을 여는 주소 (본문을 읽어 올 때 쓴다)
_LINK = re.compile(r"['\"](/ilos/[\w./-]*_view[\w./-]*\.acl\?[^'\"]*)['\"]")
# 연도를 채울 때, 이만큼 앞선 날짜까지는 올해로 본다
_FUTURE_SLACK = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class TodoRow:
    """할 일 목록 한 줄 (과제·시험·온라인 강의 기한)."""

    kjkey: str
    seq: str
    category: str
    course: str
    title: str
    due_at: datetime | None = None

    @property
    def item_id(self) -> str:
        return f"eclass:{self.category}:{self.kjkey}:{self.seq}"


@dataclass(frozen=True, slots=True)
class CourseRow:
    kjkey: str
    name: str
    professor: str = ""
    time: str = ""


@dataclass(frozen=True, slots=True)
class ListRow:
    """게시판 한 줄. 공지·자료·게시판·쪽지가 대체로 같은 모양이다."""

    article_id: str
    title: str
    posted_at: datetime | None = None
    author: str = ""
    # 글을 여는 주소. 본문을 읽어 올 때 쓴다 (목록 줄의 onclick에 들어 있다).
    view_url: str = ""


@dataclass(frozen=True, slots=True)
class NoticeRow:
    article_id: str
    title: str
    course: str = ""
    posted_on: datetime | None = None

    @property
    def item_id(self) -> str:
        return f"eclass:notice:{self.course or 'common'}:{self.article_id}"


@dataclass(frozen=True, slots=True)
class ParseResult:
    rows: list = field(default_factory=list)
    # 형태가 달라 건너뛴 줄 수. 전부 건너뛰었으면 사이트 구조 변경을 의심한다.
    skipped: int = 0

    @property
    def suspicious(self) -> bool:
        return self.skipped > 0 and not self.rows


def parse_datetime(text: str | None) -> datetime | None:
    """'2026.04.08 오후 11:59' 같은 표기를 Asia/Seoul 시각으로 바꾼다. 여러 개면 마지막 것."""
    if not text:
        return None
    matches = _DATE.findall(text)
    if not matches:
        return None
    year, month, day, meridiem, hour, minute = matches[-1]
    hour_value = int(hour) if hour else 0
    if meridiem == "오후" and hour_value < 12:
        hour_value += 12
    elif meridiem == "오전" and hour_value == 12:
        hour_value = 0
    try:
        return datetime(int(year), int(month), int(day), hour_value, int(minute or 0), tzinfo=KST)
    except ValueError:
        return None


def _text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def parse_todo_list(html: str) -> ParseResult:
    """할 일 목록(과제·시험·온라인 강의 기한)."""
    soup = BeautifulSoup(html, "html.parser")
    rows: list[TodoRow] = []
    skipped = 0
    for block in soup.select(".todo_wrap"):
        if "no_data" in (block.get("class") or []):
            continue  # "할 일이 없습니다" 자리 표시
        title = _text(block.select_one(".todo_title"))
        onclick = " ".join(
            str(node.get("onclick", "")) for node in [block, *block.find_all(attrs={"onclick": True})]
        )
        call = _GO_LECTURE.search(onclick)
        gubun = block.select_one("input[id^=gubun_]")
        kj = block.select_one("input[id^=kj_]")

        kjkey = (call.group(1) if call else "") or (kj.get("value", "") if kj else "")
        seq = call.group(2) if call else ""
        raw_category = (call.group(3) if call else "") or (gubun.get("value", "") if gubun else "")
        if not (title and kjkey and seq):
            skipped += 1
            continue

        dates = [_text(node) for node in block.select(".todo_date")]
        rows.append(
            TodoRow(
                kjkey=kjkey,
                seq=seq,
                category=CATEGORIES.get(raw_category.strip().lower(), DEFAULT_CATEGORY),
                course=_text(block.select_one(".todo_subjt")),
                title=title,
                due_at=parse_datetime(dates[-1] if dates else ""),
            )
        )
    return ParseResult(rows, skipped)


def parse_course_select(html: str) -> ParseResult:
    """할 일 화면의 과목 선택 상자에서 수강 과목을 읽는다.

    option 값은 "KJKEY||L", 글자는 "[YYYY년 N학기] 과목명" 꼴이다.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[CourseRow] = []
    skipped = 0
    for option in soup.select("#todo_select option"):
        value = (option.get("value") or "").strip()
        if not value:
            continue  # "전체 과목 보기"
        label = _text(option)
        name = label.split("]", 1)[1].strip() if "]" in label else label
        if not name or len(name) < 2:
            skipped += 1
            continue
        rows.append(CourseRow(kjkey=value.split("||")[0], name=name, time=_term(label)))
    return ParseResult(rows, skipped)


def _term(label: str) -> str:
    """'[YYYY년 N학기] 과목명' → 'YYYY년 N학기'"""
    if label.startswith("[") and "]" in label:
        return " ".join(label[1 : label.index("]")].split())
    return ""


def parse_courses(html: str) -> ParseResult:
    """수강 과목 목록."""
    soup = BeautifulSoup(html, "html.parser")
    rows: list[CourseRow] = []
    skipped = 0
    for block in soup.select(".content-container"):
        name = _text(block.select_one(".content-title"))
        onclick = " ".join(
            str(node.get("onclick", "")) for node in [block, *block.find_all(attrs={"onclick": True})]
        )
        found = _ECLASS_ROOM.search(onclick)
        if not (name and found):
            skipped += 1
            continue
        authors = [_text(node) for node in block.select(".content-author li")]
        rows.append(
            CourseRow(
                kjkey=found.group(1),
                name=name,
                professor=authors[0] if authors else "",
                time=authors[1] if len(authors) > 1 else "",
            )
        )
    return ParseResult(rows, skipped)


def parse_short_datetime(text: str | None, now: datetime) -> datetime | None:
    """연도 없이 적힌 날짜. 앞으로 한 달 넘게 남은 날로 읽히면 지난해 글로 본다."""
    if not text:
        return None
    found = _SHORT_DATE.search(text)
    if not found:
        return None
    month, day, meridiem, hour, minute = found.groups()
    hour_value = int(hour) if hour else 0
    if meridiem == "오후" and hour_value < 12:
        hour_value += 12
    elif meridiem == "오전" and hour_value == 12:
        hour_value = 0
    local = now.astimezone(KST)
    for year in (local.year, local.year - 1):
        try:
            when = datetime(year, int(month), int(day), hour_value, int(minute or 0), tzinfo=KST)
        except ValueError:
            return None
        if when - local <= _FUTURE_SLACK:
            return when
    return None


def _row_id(markup: str, row) -> str:
    """줄 하나를 가리키는 번호. 화면마다 이름이 달라 알려진 것을 차례로 본다."""
    found = _ROW_ID.search(markup) or _VIEW_PAGE.search(markup) or _IMPT_SEQ.search(markup)
    if found:
        return found.group(1)
    box = row.select_one("input[type=checkbox][value]")
    return str(box.get("value", "")).strip() if box else ""


def _row_title(row) -> str:
    """제목. 아래쪽 작성자·조회수 줄은 빼고 첫 줄만 쓴다."""
    for selector in (".subjt_top", "a div", "td.left a", "td.left", "a"):
        node = row.select_one(selector)
        if node is not None and (text := _text(node)):
            return text
    return ""


def parse_list(html: str, now: datetime | None = None) -> ParseResult:
    """게시판 목록 한 판. 공지·자료실·열린 게시판·쪽지가 모두 이 모양이다.

    칸이 둘 이상인 줄만 센다. "조회할 자료가 없습니다"는 칸 하나를 늘려 쓰므로 저절로 빠진다.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[ListRow] = []
    skipped = 0
    seen: set[str] = set()
    for row in soup.select("tr"):
        cells = row.select("td")
        if len(cells) < 2:
            continue
        markup = str(row)
        article_id = _row_id(markup, row)
        title = _row_title(row)
        if not (article_id and title) or article_id in seen:
            skipped += 1
            continue
        seen.add(article_id)

        posted_at = None
        for cell in reversed(cells):
            text = _text(cell)
            posted_at = parse_datetime(text) or (parse_short_datetime(text, now) if now else None)
            if posted_at is not None:
                break
        link = _LINK.search(markup)
        rows.append(
            ListRow(
                article_id=article_id,
                title=title,
                posted_at=posted_at,
                author=_text(row.select_one(".subjt_bottom span")),
                view_url=link.group(1).replace("&amp;", "&") if link else "",
            )
        )
    return ParseResult(rows, skipped)


def parse_body(html: str) -> str:
    """글 본문. 외부에서 온 글이므로 읽어 두기만 한다 (절대 규칙 8)."""
    soup = BeautifulSoup(html, "html.parser")
    for selector in (".textviewer", "#content_text"):
        node = soup.select_one(selector)
        if node is not None and (text := _text(node)):
            return text
    return ""


def parse_notices(html: str, course: str = "") -> ParseResult:
    """공지 목록. 표(tr) 안의 ARTL_NUM 링크를 기준으로 읽는다."""
    soup = BeautifulSoup(html, "html.parser")
    rows: list[NoticeRow] = []
    skipped = 0
    seen: set[str] = set()
    for row in soup.select("tr"):
        markup = str(row)
        found = _ARTL_NUM.search(markup)
        if not found:
            continue
        link = row.select_one("a") or row
        title = _text(link)
        if not title or found.group(1) in seen:
            skipped += 1
            continue
        seen.add(found.group(1))
        rows.append(
            NoticeRow(
                article_id=found.group(1),
                title=title,
                course=course,
                posted_on=parse_datetime(_text(row)),
            )
        )
    return ParseResult(rows, skipped)
