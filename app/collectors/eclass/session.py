"""eClass 브라우저 세션 (Playwright).

- **학교 계정 비밀번호는 이 모듈 밖으로 나가지 않는다.** 로그·예외 메시지·이벤트에 담지 않는다 (절대 규칙 7).
- 로그인 세션은 private/ 안에 저장해 재사용하고, 만료됐을 때만 다시 로그인한다.
- 실패는 종류를 나눠 보고한다: 로그인 실패 / 추가 인증·CAPTCHA / 구조 변경 / 네트워크.
- 조회만 한다. 과제 제출·파일 업로드는 만들지 않는다 (절대 규칙 5).
"""

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from app.core.config import EclassSettings

logger = logging.getLogger(__name__)

LOGIN_PATH = "/ilos/main/member/login_form.acl"
MAIN_PATH = "/ilos/main/main_form.acl"
# 할 일 목록은 메인 화면이 AJAX로 부르는 주소다 (2026-09-20 실제 확인)
TODO_PATH = "/ilos/mp/todo_list_form.acl"
COURSE_LIST_PATH = "/ilos/st/main/course_ing_list_form.acl"
# 과목방은 열쇠(KJKEY)를 넘겨 문을 연 뒤에야 안이 보인다 (docs/refs/eclass-paths.md)
COURSE_ENTER_PATH = "/ilos/st/course/eclass_room2.acl"
COURSE_ROOM_PATH = "/ilos/st/course/submain_form.acl"
NOTICE_PATH = "/ilos/community/notice_list_form.acl"
ACADEMIC_CALENDAR_PATH = "/ilos/st/schedule/academic_calendar_list_form.acl"

ID_FIELD = "#usr_id"
PASSWORD_FIELD = "#usr_pwd"
# 로그인 버튼은 <div onclick="loginForm();">이라 Enter(폼 제출)로는 로그인되지 않는다
LOGIN_BUTTON = '[onclick*="loginForm"]'
# 로그인한 화면에만 나오는 표시. 주소만으로는 로그인 여부를 알 수 없다.
LOGGED_IN_MARKS = ("logout.acl", "로그아웃")
# 추가 인증이 걸린 신호 (로그인 화면에 reCAPTCHA가 나타난다)
CAPTCHA_MARKS = ("recaptcha", "그림문자", "자동입력 방지", "captcha")
PAGE_TIMEOUT_MS = 20_000

# 화면 안에서 보내는 AJAX 요청 (세션과 헤더를 그대로 쓴다)
_AJAX_POST = """async ({url, body}) => {
    const response = await fetch(url, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-Requested-With': 'XMLHttpRequest',
        },
        body,
    });
    return await response.text();
}"""


class Failure(StrEnum):
    LOGIN = "login"
    CAPTCHA = "captcha"
    LAYOUT = "layout"
    NETWORK = "network"


class EclassError(Exception):
    """수집 실패. reason으로 종류를 구분하고, 메시지에 계정 정보를 담지 않는다."""

    def __init__(self, reason: Failure, message: str) -> None:
        super().__init__(message)
        self.reason = reason


MESSAGES = {
    Failure.LOGIN: "학교 계정 로그인에 실패했습니다. private/.env의 ECLASS_ID·ECLASS_PASSWORD를 확인해 주세요.",
    Failure.CAPTCHA: "eClass가 추가 인증(자동입력 방지)을 요구해 자동 수집을 멈췄습니다. 직접 한 번 로그인해 주세요.",
    Failure.LAYOUT: "eClass 화면 구조가 달라져 내용을 읽지 못했습니다.",
    Failure.NETWORK: "eClass에 연결하지 못했습니다.",
}


def logged_in(html: str) -> bool:
    """로그인된 화면인지. 실패해도 메인 주소로 보내 주는 경우가 있어 화면 표시로 판단한다."""
    return any(mark in html for mark in LOGGED_IN_MARKS)


def classify_login(url: str, html: str) -> Failure | None:
    """로그인 후 도착한 화면으로 성패를 가린다. 성공이면 None."""
    lowered = html.lower()
    if any(mark in lowered for mark in CAPTCHA_MARKS):
        return Failure.CAPTCHA
    if logged_in(html):
        return None
    if LOGIN_PATH in url or ID_FIELD.strip("#") in html:
        return Failure.LOGIN
    return Failure.LAYOUT


def logged_out(url: str, html: str) -> bool:
    """세션이 끊겼는지. 로그인 표시가 없으면 끊긴 것으로 본다."""
    return not logged_in(html)


@dataclass(slots=True)
class EclassSession:
    """열고 닫는 것은 호출한 쪽(수집기)이 관리한다."""

    settings: EclassSettings
    _playwright: Any = None
    _browser: Any = None
    _context: Any = None
    _page: Any = None

    async def __aenter__(self) -> "EclassSession":
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - 설치 안내용
            raise EclassError(Failure.LAYOUT, "playwright가 설치되어 있지 않습니다.") from exc

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        state = self.settings.session_file
        self._context = await self._browser.new_context(
            storage_state=str(state) if state.exists() else None,
            locale="ko-KR",
            timezone_id="Asia/Seoul",
        )
        self._context.set_default_timeout(PAGE_TIMEOUT_MS)
        self._page = await self._context.new_page()

    async def close(self) -> None:
        for resource in (self._context, self._browser):
            if resource is not None:
                await resource.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._playwright = self._browser = self._context = self._page = None

    def url_for(self, path: str) -> str:
        return urljoin(self.settings.eclass_url, path)

    async def ensure_login(self) -> None:
        """이미 로그인되어 있으면 그대로 두고, 아니면 로그인한다."""
        html = await self.open(MAIN_PATH)
        if not logged_out(self._page.url, html):
            return
        await self._login()

    async def _login(self) -> None:
        await self.open(LOGIN_PATH)
        try:
            await self._page.fill(ID_FIELD, self.settings.username)
            # 비밀번호는 여기서만 쓰인다
            await self._page.fill(PASSWORD_FIELD, self.settings.password)
            await self._page.click(LOGIN_BUTTON)
            await self._page.wait_for_load_state("networkidle")
        except Exception as exc:
            raise EclassError(Failure.LAYOUT, MESSAGES[Failure.LAYOUT]) from _hide(exc)

        reason = classify_login(self._page.url, await self._page.content())
        if reason is not None:
            raise EclassError(reason, MESSAGES[reason])
        await self._save_session()
        logger.info("eClass 로그인 성공")

    async def _save_session(self) -> None:
        path: Path = self.settings.session_file
        path.parent.mkdir(parents=True, exist_ok=True)
        await self._context.storage_state(path=str(path))

    async def open(self, path: str) -> str:
        try:
            await self._page.goto(self.url_for(path), wait_until="domcontentloaded")
            return await self._page.content()
        except Exception as exc:
            raise EclassError(Failure.NETWORK, MESSAGES[Failure.NETWORK]) from _hide(exc)

    async def enter_course(self, key: str) -> None:
        """과목방 문을 연다. 그 과목을 현재 방으로 삼을 뿐 아무것도 바꾸지 않는다 (절대 규칙 5)."""
        await self.post(
            COURSE_ENTER_PATH,
            {"KJKEY": key, "returnData": "json", "returnURI": COURSE_ROOM_PATH, "encoding": "utf-8"},
        )
        await self.open(COURSE_ROOM_PATH)

    async def post(self, path: str, data: dict[str, str]) -> str:
        """할 일 목록처럼 AJAX로 받아야 하는 화면.

        브라우저 밖에서 부르면 세션이 끊긴 것으로 취급되므로, 열려 있는 화면 안에서 같은 방식으로 요청한다.
        """
        body = "&".join(f"{key}={value}" for key, value in data.items())
        try:
            return await self._page.evaluate(_AJAX_POST, {"url": self.url_for(path), "body": body})
        except Exception as exc:
            raise EclassError(Failure.NETWORK, MESSAGES[Failure.NETWORK]) from _hide(exc)


def _hide(exc: Exception) -> None:
    """원래 예외에는 URL·입력값이 섞일 수 있어 연결하지 않고 종류만 남긴다."""
    logger.info("eClass 작업 실패: %s", type(exc).__name__)
    return None
