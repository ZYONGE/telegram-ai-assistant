"""eClass 화면을 비서가 읽을 글로 바꾼다 (순수 함수).

비서가 그 자리에서 여는 화면(`browse.py`)을 위한 것이다. 수집기의 파서(`parse.py`)와 달리
화면마다 전용 파서를 두지 않고, 표·상세·본문 세 모양을 두루 읽는다.

- **조회만 한다.** 화면에서 주운 주소는 `is_read_only`를 통과한 것만 연다.
  과제 제출, 시험 응시, 글쓰기, 파일 내려받기, 강의 재생(출석이 찍힌다)은 열지 않는다 (절대 규칙 5).
- 화면 글자는 외부에서 온 데이터다. 읽어 옮기기만 하고 지시로 다루지 않는다 (절대 규칙 8).
- 화면 안 스크립트에 학번 같은 값이 들어 있다. 요청에만 쓰고 결과 글·로그에 옮기지 않는다.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

# 바꾸거나 내려받거나 기록을 남기는 주소에 들어가는 조각. 주소 경로에 하나라도 있으면 열지 않는다.
UNSAFE_WORDS = (
    "submit", "insert", "update", "delete", "remove", "save", "modify", "write", "regist", "apply",
    "cancel", "upload", "proc", "exec", "down", "attach", "logout", "start", "take", "answer",
    "exam", "vote", "reply", "cmmt", "comment", "join", "request", "button", "check",
    # 강의 재생 화면은 여는 것만으로 수강 기록이 남는다
    "online_view", "play", "viewer", "movie", "vod", "navi",
)
# 조회 화면의 이름 모양. 이 중 하나로 끝나는 화면만 연다.
READ_MARKS = ("_list", "_view", "plan_form", "submain_form", "faq_form", "timetable_form")

_ONCLICK_LINK = re.compile(r"(?:pageMove|location\.href\s*=)\s*\(?\s*['\"](/ilos/[^'\"]+)['\"]")
_MESSAGE_LINK = re.compile(r"viewPage\(\s*['\"](\d+)['\"]\s*,\s*['\"](\w+)['\"]")
MESSAGE_VIEW = "/ilos/message/received_view_pop_form.acl"
_ATTACHED = re.compile(r"첨부파일\s*\(\d+개\).*$")
# 목록에서 사람에게 뜻이 없는 칸
_SKIP_HEADS = frozenset({"번호", "중요", "선택", "순번", ""})
_TITLE_HEADS = ("제목", "주제")
MAX_ROWS = 30
MAX_TEXT = 3500


def is_read_only(url: str) -> bool:
    """조회 화면 주소인지. 경로만 본다 (질의에 `start=` 같은 쪽 번호가 흔히 붙는다)."""
    parts = urlsplit(url)
    if parts.scheme or parts.netloc:
        return False  # 화면 안 주소는 늘 경로만 쓴다. 다른 사이트로 가는 주소는 열지 않는다.
    path = parts.path.lower()
    if not path.startswith("/ilos/") or not path.endswith(".acl"):
        return False
    if any(word in path for word in UNSAFE_WORDS):
        return False
    name = path.rsplit("/", 1)[-1]
    return any(mark in name for mark in READ_MARKS)


@dataclass(frozen=True, slots=True)
class ListRequest:
    """껍데기 화면이 줄을 채우려고 부르는 요청."""

    url: str
    # 화면 스크립트에 적힌 그대로의 값 (학번·과목 열쇠가 들어 있다. 요청에만 쓴다)
    fields: dict[str, str] = field(default_factory=dict)
    # 스크립트가 실행 때 채우는 칸. 쪽 번호·주차 말고는 비워 보낸다.
    dynamic: tuple[str, ...] = ()

    def data(self, page: int = 1, week: str = "") -> dict[str, str]:
        values = dict(self.fields)
        for name in self.dynamic:
            if name == "start":
                values[name] = str(page)
            elif name == "WEEK_NO":
                values[name] = week
            else:
                values[name] = ""
        values.setdefault("encoding", "utf-8")
        return values


def list_request(html: str, shell_path: str) -> ListRequest | None:
    """껍데기(`..._list_form.acl`)가 부르는 내용 주소(`..._list.acl`)와 보낼 값을 스크립트에서 읽는다.

    내용 요청은 과목 열쇠·학번을 함께 보내야 받아 준다 (출석·온라인강의). 값을 코드에 적지 않고 화면에서 읽는다.
    """
    target = urlsplit(shell_path).path.replace("_form.acl", ".acl")
    if target == urlsplit(shell_path).path:
        return None
    found = re.search(r"url\s*:\s*['\"]" + re.escape(target) + r"['\"]", html)
    if found is None:
        return None
    block = re.search(r"data\s*:\s*\{([^{}]*)\}", html[found.end() : found.end() + 1500])
    fields: dict[str, str] = {}
    dynamic: list[str] = []
    if block is not None:
        for name, value in re.findall(r"(\w+)\s*:\s*([^,\n]+)", block.group(1)):
            value = value.strip()
            literal = re.fullmatch(r"(['\"])(.*)\1", value)
            if literal:
                fields[name] = literal.group(2)
            else:
                dynamic.append(name)
    return ListRequest(target, fields, tuple(dynamic))


@dataclass(frozen=True, slots=True)
class Week:
    number: str
    label: str
    current: bool = False


def parse_weeks(html: str) -> list[Week]:
    """온라인강의 주차. 화면이 고른 주(`wb-choice`), 없으면 열린 첫 주(`wb-on`)를 지금 주로 본다."""
    soup = BeautifulSoup(html, "html.parser")
    weeks: list[Week] = []
    for node in soup.select(".wb[id^=week-]"):
        classes = node.get("class") or []
        weeks.append(Week(str(node.get("id"))[5:], _text(node), "wb-choice" in classes))
    if weeks and not any(week.current for week in weeks):
        opened = next((i for i, node in enumerate(soup.select(".wb[id^=week-]")) if "wb-on" in (node.get("class") or [])), 0)
        weeks[opened] = Week(weeks[opened].number, weeks[opened].label, True)
    return weeks


@dataclass(frozen=True, slots=True)
class PageRow:
    text: str
    # 글을 여는 주소 (조회 화면인 것만)
    link: str = ""


@dataclass(frozen=True, slots=True)
class Page:
    rows: list[PageRow] = field(default_factory=list)
    # 표가 아닌 화면의 글 (상세·강의계획서·성적 안내 등)
    text: str = ""
    attachments: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.rows and not self.text


def read_page(html: str) -> Page:
    """화면 하나를 읽는다. 목록 표가 있으면 줄로, 없으면 상세·본문 글로."""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style"]):
        node.decompose()
    attachments = _attachments(soup)
    if soup.select_one("table.bbsview, .textviewer") is not None:
        # 상세 화면. 시험의 응시 기록처럼 곁에 붙은 작은 표도 함께 읽는다.
        extra = [row.text for table in _plain_tables(soup) for row in _rows(table)]
        return Page(text="\n".join([_detail(soup), *extra]).strip(), attachments=attachments)
    table = next(iter(_plain_tables(soup)), None)
    if table is not None:
        rows = _rows(table)
        if rows:
            return Page(rows=rows, attachments=attachments)
        # 머리글만 있고 줄이 없다: "조회할 자료가 없습니다"
        empty = " ".join(_text(cell) for cell in table.select("td")) or "글이 없습니다."
        return Page(text=empty, attachments=attachments)
    return Page(text=_detail(soup), attachments=attachments)


def _plain_tables(soup) -> list:
    """머리글이 있는 표. 상세 화면의 이름·값 표(`bbsview`)는 목록이 아니다."""
    return [
        table
        for table in soup.select("table")
        if "bbsview" not in (table.get("class") or []) and table.select("th")
    ]


def _rows(table) -> list[PageRow]:
    heads = [_text(node) for node in table.select("tr th")]
    title_index = next((heads.index(name) for name in _TITLE_HEADS if name in heads), None)
    rows: list[PageRow] = []
    for row in table.select("tr"):
        cells = row.select("td")
        if len(cells) < 2:
            continue
        parts: list[str] = []
        if title_index is not None and title_index < len(cells):
            parts.append(_title(cells[title_index]))
        for index, cell in enumerate(cells):
            head = heads[index] if index < len(heads) else ""
            if index == title_index or head in _SKIP_HEADS:
                continue
            value = _cell(cell)
            if value:
                parts.append(f"{head} {value}" if head else value)
        parts = [part for part in parts if part]
        if parts:
            rows.append(PageRow(" · ".join(parts), _row_link(row)))
        if len(rows) >= MAX_ROWS:
            break
    return rows


def _title(cell) -> str:
    """제목 칸. 아래쪽 작성자·조회수 줄은 따로 붙인다."""
    top = cell.select_one(".subjt_top") or cell.select_one("a div") or cell
    title = _text(top)
    bottom = _text(cell.select_one(".subjt_bottom"))
    return f"{title} ({bottom})" if bottom and top is not cell else title


def _cell(cell) -> str:
    """칸 글자. 제출 여부처럼 그림으로만 표시하는 칸은 그림 설명을 읽는다."""
    text = _text(cell)
    if not text and (image := cell.select_one("img")) is not None:
        text = str(image.get("alt") or image.get("title") or "").strip()
    # 첨부 칸. 내려받지 않으므로 있다는 것만 알린다.
    return "있음" if "다운로드" in text else text


def _row_link(row) -> str:
    markup = str(row)
    found = _ONCLICK_LINK.search(markup)
    link = found.group(1).replace("&amp;", "&") if found else ""
    if not link:
        anchor = row.select_one("a[href^='/ilos/']")
        link = str(anchor.get("href")) if anchor else ""
    if not link and (message := _MESSAGE_LINK.search(markup)):
        link = f"{MESSAGE_VIEW}?SEQ={message.group(1)}&SEND_ID={message.group(2)}"
    return link if link and is_read_only(link) else ""


def _detail(soup) -> str:
    """상세 화면: 이름·값 표 + 본문. 그것도 없으면 본문 자리의 글 전체."""
    lines: list[str] = []
    for row in soup.select("table.bbsview tr"):
        cells = row.select("th, td")
        for index in range(0, len(cells) - 1, 2):
            name, value = _text(cells[index]), _text(cells[index + 1])
            if name and value:
                lines.append(f"{name}: {value}")
    body = soup.select_one(".textviewer")
    if body is not None:
        lines.append(_ATTACHED.sub("", _text(body)).strip())
    if not any(lines):
        area = soup.select_one("#content_text") or soup.select_one("#contents") or soup.body or soup
        lines.append(_text(area))
    text = "\n".join(line for line in lines if line)
    return text[:MAX_TEXT] + ("…" if len(text) > MAX_TEXT else "")


def _attachments(soup) -> list[str]:
    """첨부 파일 이름과 크기만. 내려받는 주소는 따라가지 않는다."""
    names = [
        re.sub(r"^[-·\s]+", "", _text(link))
        for link in soup.select('a[href*="efile_download"], a[href*="file_down"]')
    ]
    return list(dict.fromkeys(name for name in names if name))


def _text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""
