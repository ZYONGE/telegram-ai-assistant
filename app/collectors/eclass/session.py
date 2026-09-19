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
LOGIN_ACTION = "/ilos/lo/login.acl"
MAIN_PATH = "/ilos/main/main_form.acl"
TODO_PATH = "/ilos/mp/todo_list.acl"
COURSE_LIST_PATH = "/ilos/mp/course_register_list_form.acl"
NOTICE_PATH = "/ilos/community/notice_list_form.acl"
ACADEMIC_CALENDAR_PATH = "/ilos/st/schedule/academic_calendar_list_form.acl"

ID_FIELD = "#usr_id"
PASSWORD_FIELD = "#usr_pwd"
# 추가 인증이 걸린 신호 (로그인 화면에 reCAPTCHA가 나타난다)
CAPTCHA_MARKS = ("recaptcha", "그림문자", "자동입력 방지", "captcha")
PAGE_TIMEOUT_MS = 20_000


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


def classify_login(url: str, html: str) -> Failure | None:
    """로그인 후 도착한 화면으로 성패를 가린다. 성공이면 None."""
    lowered = html.lower()
    if any(mark in lowered for mark in CAPTCHA_MARKS):
        return Failure.CAPTCHA
    if MAIN_PATH in url:
        return None
    if LOGIN_PATH in url or "login" in url.lower():
        return Failure.LOGIN
    return Failure.LAYOUT


def logged_out(url: str, html: str) -> bool:
    """세션이 끊겨 로그인 화면으로 돌아왔는지."""
    return LOGIN_PATH in url or ID_FIELD.strip("#") in html


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
            await self._page.press(PASSWORD_FIELD, "Enter")
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

    async def post(self, path: str, data: dict[str, str]) -> str:
        """할 일 목록처럼 POST로 받아야 하는 화면. 응답 본문을 그대로 돌려준다."""
        try:
            response = await self._context.request.post(self.url_for(path), form=data)
            if not response.ok:
                raise EclassError(Failure.NETWORK, f"{MESSAGES[Failure.NETWORK]} (HTTP {response.status})")
            return await response.text()
        except EclassError:
            raise
        except Exception as exc:
            raise EclassError(Failure.NETWORK, MESSAGES[Failure.NETWORK]) from _hide(exc)


def _hide(exc: Exception) -> None:
    """원래 예외에는 URL·입력값이 섞일 수 있어 연결하지 않고 종류만 남긴다."""
    logger.info("eClass 작업 실패: %s", type(exc).__name__)
    return None
