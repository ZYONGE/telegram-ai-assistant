"""eClass를 그 자리에서 열어 보는 창구. 비서의 도구(`app/tools/eclass_browse.py`)가 쓴다.

수집기는 정해 둔 화면만 주기적으로 모은다. 이 창구는 물어본 것을 **지금** 열어 본다:
과목 목록, 과목방의 모든 메뉴(공지·과제·시험·강의자료·온라인강의·출석·성적·강의계획서 등),
공통 화면(학교 공지·알림·쪽지·할 일·시간표), 그리고 목록에서 고른 글 하나.

- **조회만 한다.** 과제 제출, 시험 응시, 글쓰기, 파일 내려받기, 강의 재생은 하지 않는다 (절대 규칙 5).
  화면에서 주운 주소는 `page.is_read_only`를 통과한 것만 연다.
- 비밀번호는 세션 안에서만 쓴다. 도구 결과에 들어가는 것은 화면 글자뿐이다 (절대 규칙 7).
- 수집기와 **같은 세션 쿠키**를 쓰므로, 과목방 문을 여닫는 순서가 섞이지 않게 잠금을 함께 쓴다.
  (한쪽이 A 과목에 들어간 사이 다른 쪽이 B 과목 문을 열면 A의 목록 자리에 B가 온다.)
- 로그인이 막혀 수집을 멈춘 상태면 이 창구도 로그인을 다시 시도하지 않는다. 계정이 잠길 수 있다.
"""

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from app.collectors.documents import MAX_BYTES
from app.collectors.eclass.page import (
    ListRequest,
    Page,
    PageRow,
    FILE_LIST_PATH,
    Week,
    ajax_request,
    is_download,
    is_read_only,
    list_request,
    parse_weeks,
    read_files,
    read_page,
)
from app.collectors.eclass.parse import CourseRow, parse_course_select, parse_todo_list
from app.collectors.eclass.session import MAIN_PATH, TODO_PATH, EclassError, EclassSession, Failure
from app.collectors.eclass.sources.todo import TODO_FORM_DATA, TODO_ROWS_DATA, TODO_ROWS_PATH
from app.core.clock import format_kst, utc_now
from app.core.config import EclassSettings
from app.storage.eclass import EclassHealthStore

logger = logging.getLogger(__name__)

COURSE = "/ilos/st/course/"
# 과목방 메뉴 → 화면. 이름은 eClass 메뉴 글자 그대로다.
COURSE_MENUS: dict[str, str] = {
    "공지사항": COURSE + "notice_list_form.acl",
    "과제": COURSE + "report_list_form.acl",
    "시험": COURSE + "test_list_form.acl",
    "강의자료": COURSE + "lecture_material_list_form.acl",
    "온라인강의": COURSE + "online_list_form.acl",
    "실시간강의": COURSE + "zoom_list_form.acl",
    "출석": COURSE + "attendance_list_form.acl",
    "성적": COURSE + "eval3_result_view_form.acl",
    "강의계획서": COURSE + "plan_form.acl",
    "팀프로젝트": COURSE + "project_list_form.acl",
    "토론": COURSE + "discuss_list_form.acl",
    "설문": COURSE + "survey2_list_form.acl",
    "열린게시판": COURSE + "material_list_form.acl",
    "질의응답": COURSE + "qna2_faq_form.acl",
}
# 과목과 상관없는 화면 → (먼저 여는 화면, 줄을 받는 주소, 보낼 값)
COMMON_MENUS: dict[str, tuple[str, str, dict[str, str]]] = {
    "학교공지": ("/ilos/community/notice_list_form.acl", "", {}),
    "알림": (MAIN_PATH, "/ilos/mp/notification_list.acl", {"display": "10", "OPEN_DTM": ""}),
    "쪽지": ("/ilos/message/received_list_pop_form.acl", "", {}),
    "시간표": ("/ilos/st/main/pop_academic_timetable_form.acl", "", {}),
    "할일": (MAIN_PATH, "", {}),
}
MENUS = (*COURSE_MENUS, *COMMON_MENUS)
# 수강 과목은 학기 중에 바뀌지 않는다. 물을 때마다 읽지 않는다.
COURSES_TTL = timedelta(hours=6)
# 기억해 둘 글 번호 수. 넘치면 오래된 것부터 잊는다 (다시 목록을 열면 된다).
MAX_LINKS = 400
BLOCKED = "로그인이 연속으로 실패해 eClass 자동 확인을 멈춘 상태입니다. 계정을 확인하고 다시 켜야 열 수 있습니다."


class BrowseError(ValueError):
    """찾는 과목·메뉴·글이 없을 때. 메시지는 비서가 사용자에게 그대로 옮길 수 있게 쓴다."""


@dataclass(frozen=True, slots=True)
class Link:
    url: str
    # 과목방 안의 글이면 그 과목 열쇠. 그 방에 들어가야 열린다.
    course_key: str = ""
    title: str = ""


@dataclass(slots=True)
class Result:
    heading: str
    page: Page
    # 줄마다 붙인 글 번호 (page.rows와 같은 순서, 열 수 없는 줄은 빈 칸)
    refs: list[str] = field(default_factory=list)
    weeks: list[Week] = field(default_factory=list)
    # 첨부 파일 번호 (page.files와 같은 순서)
    file_refs: list[str] = field(default_factory=list)


class EclassBrowser:
    def __init__(
        self,
        settings: EclassSettings,
        lock: asyncio.Lock,
        health: EclassHealthStore | None = None,
        session_factory: Callable[[EclassSettings], object] = EclassSession,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._lock = lock
        self._health = health
        self._session_factory = session_factory
        self._clock = clock
        self._courses: list[CourseRow] = []
        self._courses_at: datetime | None = None
        self._links: OrderedDict[str, Link] = OrderedDict()
        self._next = 1

    async def courses(self) -> list[CourseRow]:
        async with self._open() as session:
            return await self._course_list(session)

    async def open_menu(self, menu: str, course: str = "", page: int = 1, week: str = "") -> Result:
        menu = _menu_name(menu)
        async with self._open() as session:
            if menu in COMMON_MENUS:
                return self._result(menu, await self._common(session, menu, page), "")
            if not course:
                raise BrowseError(f"'{menu}'은 과목방 메뉴입니다. 어느 과목인지 알려 주세요.")
            target = _pick(await self._course_list(session), course)
            await session.enter_course(target.kjkey)
            shell_path = COURSE_MENUS[menu]
            shell = await session.open(shell_path)
            weeks = parse_weeks(shell) if menu == "온라인강의" else []
            chosen = week or next((item.number for item in weeks if item.current), "")
            request = list_request(shell, shell_path)
            html = await session.post(request.url, request.data(page, chosen)) if request else shell
            result = self._result(f"{target.name} · {menu}", read_page(html), target.kjkey)
            result.weeks = weeks
            if chosen and weeks:
                result.heading += f" · {chosen}주차"
            return result

    async def read_file(self, ref: str) -> tuple[str, bytes]:
        """첨부 파일 하나를 받는다. 파일 번호는 글을 열었을 때 붙는다 (f1, f2 …)."""
        link = self._links.get(ref.strip().lower())
        if link is None or not ref.strip().lower().startswith("f"):
            raise BrowseError(f"{ref} 첨부 파일을 찾지 못했습니다. 글을 다시 열어 번호를 확인해 주세요.")
        if not is_download(link.url):
            raise BrowseError("첨부 파일 주소가 아니라 받지 않았습니다.")
        async with self._open() as session:
            if link.course_key:
                await session.enter_course(link.course_key)
            return link.title, await session.download(link.url, MAX_BYTES)

    async def read(self, ref: str) -> Result:
        link = self._links.get(ref.strip().lower())
        if link is None:
            raise BrowseError(f"{ref} 글을 찾지 못했습니다. 목록을 다시 열어 번호를 확인해 주세요.")
        if not is_read_only(link.url):
            raise BrowseError("조회 화면이 아니라 열지 않았습니다.")
        async with self._open() as session:
            if link.course_key:
                await session.enter_course(link.course_key)
            html = await session.open(link.url)
            page = read_page(html)
            # 첨부 파일 목록은 글 화면이 따로 불러 채운다
            request = ajax_request(html, FILE_LIST_PATH)
            if request is not None and not page.files:
                page = replace(page, files=read_files(await session.post(request.url, request.data())))
            return self._result(link.title, page, link.course_key)

    # --- 안쪽 ---

    def _open(self) -> "_Visit":
        return _Visit(self)

    async def _course_list(self, session) -> list[CourseRow]:
        now = self._clock()
        if self._courses and self._courses_at and now - self._courses_at < COURSES_TTL:
            return self._courses
        rows = parse_course_select(await session.open(TODO_PATH)).rows
        if not rows:
            raise EclassError(Failure.LAYOUT, "수강 과목을 읽지 못했습니다.")
        self._courses, self._courses_at = rows, now
        return rows

    async def _common(self, session, menu: str, page: int) -> Page:
        shell_path, data_path, extra = COMMON_MENUS[menu]
        if menu == "할일":
            await session.open(MAIN_PATH)
            await session.post(TODO_PATH, dict(TODO_FORM_DATA))
            return read_todo(await session.post(TODO_ROWS_PATH, dict(TODO_ROWS_DATA)))
        shell = await session.open(shell_path)
        request = ListRequest(data_path, {**extra, "start": str(page)}) if data_path else list_request(shell, shell_path)
        return read_page(await session.post(request.url, request.data(page)) if request else shell)

    def _result(self, heading: str, page: Page, course_key: str) -> Result:
        refs = [self._remember(Link(row.link, course_key, row.text.split(" · ")[0])) if row.link else "" for row in page.rows]
        files = [self._remember(Link(url, course_key, name), "f") for name, url in page.files]
        return Result(heading, page, refs, file_refs=files)

    def _remember(self, link: Link, prefix: str = "e") -> str:
        for ref, known in self._links.items():
            if known.url == link.url and known.course_key == link.course_key and ref.startswith(prefix):
                self._links.move_to_end(ref)
                return ref
        ref = f"{prefix}{self._next}"
        self._next += 1
        self._links[ref] = link
        while len(self._links) > MAX_LINKS:
            self._links.popitem(last=False)
        return ref


class _Visit:
    """잠금 → 세션 열기 → 로그인 → (일) → 세션 닫기 → 잠금 풀기."""

    def __init__(self, browser: EclassBrowser) -> None:
        self._browser = browser
        self._session = None
        self._held = False

    async def __aenter__(self):
        browser = self._browser
        if not browser._settings.enabled:
            raise BrowseError("eClass 연결이 설정되어 있지 않습니다.")
        if browser._health is not None and (await browser._health.read()).login_blocked:
            raise BrowseError(BLOCKED)
        await browser._lock.acquire()
        self._held = True
        try:
            self._session = browser._session_factory(browser._settings)
            await self._session.start()
            await self._session.ensure_login()
        except BaseException:
            await self._close()
            raise
        return self._session

    async def __aexit__(self, *_exc: object) -> None:
        await self._close()

    async def _close(self) -> None:
        try:
            if self._session is not None:
                await self._session.close()
        finally:
            self._session = None
            if self._held:
                self._held = False
                self._browser._lock.release()


def read_todo(html: str) -> Page:
    """할 일 목록을 줄로. 수집기와 같은 파서를 쓴다."""
    rows = []
    for row in parse_todo_list(html).rows:
        due = f"마감 {format_kst(row.due_at)}" if row.due_at else ""
        rows.append(PageRow(" · ".join(part for part in (row.title, row.course, row.category, due) if part)))
    return Page(rows=rows, text="" if rows else "남은 할 일이 없습니다.")


def _menu_name(menu: str) -> str:
    """띄어쓰기·표기가 조금 달라도 메뉴를 찾는다."""
    compact = menu.replace(" ", "").strip()
    aliases = {"공지": "공지사항", "과목공지": "공지사항", "전체공지": "학교공지", "알림함": "알림",
               "받은쪽지": "쪽지", "할일목록": "할일", "자료": "강의자료", "강의": "온라인강의",
               "계획서": "강의계획서", "출결": "출석", "게시판": "열린게시판", "퀴즈": "시험"}
    name = aliases.get(compact, compact)
    if name not in MENUS:
        raise BrowseError(f"'{menu}' 메뉴는 없습니다. 열 수 있는 메뉴: {', '.join(MENUS)}")
    return name


def _pick(courses: list[CourseRow], needle: str) -> CourseRow:
    """과목 이름 일부나 목록 번호로 과목 하나를 고른다."""
    text = needle.strip()
    if text.isdigit() and 1 <= int(text) <= len(courses):
        return courses[int(text) - 1]
    compact = text.replace(" ", "").lower()
    found = [course for course in courses if compact in course.name.replace(" ", "").lower()]
    if len(found) == 1:
        return found[0]
    if not found:
        raise BrowseError(f"'{needle}' 과목을 찾지 못했습니다. 수강 과목: {', '.join(c.name for c in courses)}")
    raise BrowseError(f"'{needle}'에 맞는 과목이 여럿입니다: {', '.join(c.name for c in found)}. 하나를 골라 주세요.")
