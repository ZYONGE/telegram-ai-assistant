"""eClass 세션 (httpx).

로그인도 내용도 순수 HTTP로 된다는 것을 실제 계정으로 확인하고 브라우저를 걷어냈다
(`scripts/eclass_http_probe.py`, ADR 0008, docs/tasks.md T-22·T-23).
크로미움은 뜰 때마다 수백 MB를 썼다. 서버가 메모리 8GB라 그 피크가 스왑을 부른다.

- **학교 계정 비밀번호는 이 모듈 밖으로 나가지 않는다.** 로그·예외 메시지·이벤트에 담지 않는다 (절대 규칙 7).
- 로그인 세션(쿠키)은 private/ 안에 저장해 재사용하고, 만료됐을 때만 다시 로그인한다.
- 실패는 종류를 나눠 보고한다: 로그인 실패 / 추가 인증·CAPTCHA / 구조 변경 / 네트워크.
- 조회만 한다. 과제 제출·파일 업로드는 만들지 않는다 (절대 규칙 5).
- 화면 안에서 부르는 주소(`..._list.acl`)는 그냥 부르면 "세션이 종료되었습니다"가 온다.
  `X-Requested-With`와 직전 화면 `Referer`를 붙여 화면 안 요청처럼 보낸다.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from urllib.parse import urljoin

import httpx

from app.collectors.eclass.parse import parse_login_form
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
# 로그인한 화면에만 나오는 표시. 주소만으로는 로그인 여부를 알 수 없다.
LOGGED_IN_MARKS = ("logout.acl", "로그아웃")
# 추가 인증이 걸린 신호 (로그인 화면에 reCAPTCHA가 나타난다)
CAPTCHA_MARKS = ("recaptcha", "그림문자", "자동입력 방지", "captcha")
TIMEOUT_SECONDS = 20.0
# 학교 서버에 몰아치지 않도록 요청 사이에 쉬는 시간(초)
PAUSE_SECONDS = 0.3
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
_META_CHARSET = re.compile(rb'charset=["\']?([\w-]+)', re.IGNORECASE)


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


def decode(response: httpx.Response) -> str:
    """머리글에 문자셋이 없으면 화면의 meta를 보고 읽는다. eClass는 utf-8이다."""
    if response.charset_encoding:
        return response.text
    found = _META_CHARSET.search(response.content[:2048])
    if found:
        try:
            return response.content.decode(found.group(1).decode("ascii"), errors="replace")
        except LookupError:
            pass
    return response.content.decode("utf-8", errors="replace")


@dataclass(slots=True)
class EclassSession:
    """열고 닫는 것은 호출한 쪽(수집기)이 관리한다.

    브라우저처럼 직전 화면을 `Referer`로 붙이고, 화면 안에서 부르는 주소에는
    `X-Requested-With`를 붙인다. 그렇게 해야 학교 서버가 화면 안 요청으로 받아 준다.
    """

    settings: EclassSettings
    # 시험에서 가짜 응답을 끼워 넣을 자리. 평소에는 비어 있다.
    transport: httpx.AsyncBaseTransport | None = field(default=None, compare=False)
    _client: httpx.AsyncClient | None = None
    _page_url: str = ""
    _sent: int = 0

    async def __aenter__(self) -> "EclassSession":
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            transport=self.transport,
            cookies=self._saved_cookies(),
            follow_redirects=True,
            timeout=TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"},
        )
        self._page_url = ""
        self._sent = 0

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        self._client = None

    def url_for(self, path: str) -> str:
        return urljoin(self.settings.eclass_url, path)

    async def ensure_login(self) -> None:
        """이미 로그인되어 있으면 그대로 두고, 아니면 로그인한다."""
        html = await self.open(MAIN_PATH)
        if not logged_out(self._page_url, html):
            return
        await self._login()

    async def _login(self) -> None:
        form = parse_login_form(await self.open(LOGIN_PATH))
        if form is None:
            raise EclassError(Failure.LAYOUT, MESSAGES[Failure.LAYOUT])

        # 받은 숨은 칸을 그대로 돌려보낸다. 비밀번호는 이 본문에만 들어간다.
        data = dict(form.fields)
        data[form.id_field] = self.settings.username
        data[form.password_field] = self.settings.password
        await self._request("POST", form.action or LOGIN_PATH, data=data, ajax=False)

        html = await self.open(MAIN_PATH)
        reason = classify_login(self._page_url, html)
        if reason is not None:
            raise EclassError(reason, MESSAGES[reason])
        self._save_cookies()
        logger.info("eClass 로그인 성공")

    async def enter_course(self, key: str) -> None:
        """과목방 문을 연다. 그 과목을 현재 방으로 삼을 뿐 아무것도 바꾸지 않는다 (절대 규칙 5)."""
        await self.post(
            COURSE_ENTER_PATH,
            {"KJKEY": key, "returnData": "json", "returnURI": COURSE_ROOM_PATH, "encoding": "utf-8"},
        )
        await self.open(COURSE_ROOM_PATH)

    async def open(self, path: str) -> str:
        """화면을 연다. 화면을 옮기는 요청이라 다음 요청의 Referer가 된다."""
        return decode(await self._request("GET", path, ajax=False))

    async def download(self, path: str, max_bytes: int) -> bytes:
        """첨부 파일을 받는다. 메모리에만 두고 저장하지 않는다. 너무 크면 받다가 멈춘다."""
        if self._client is None:
            raise EclassError(Failure.NETWORK, MESSAGES[Failure.NETWORK])
        headers = {"Referer": self._page_url} if self._page_url else {}
        try:
            async with self._client.stream("GET", self.url_for(path), headers=headers) as response:
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise EclassError(Failure.LAYOUT, "파일이 너무 커서 받지 않았습니다.")
                    chunks.append(chunk)
        except EclassError:
            raise
        except Exception as exc:
            raise EclassError(Failure.NETWORK, MESSAGES[Failure.NETWORK]) from _hide(exc)
        return b"".join(chunks)

    async def post(self, path: str, data: dict[str, str]) -> str:
        """화면 안에서 부르는 주소. 그냥 부르면 세션이 끊긴 것으로 취급된다."""
        return decode(await self._request("POST", path, data=data, ajax=True))

    async def _request(
        self, method: str, path: str, *, ajax: bool, data: dict[str, str] | None = None
    ) -> httpx.Response:
        if self._client is None:
            raise EclassError(Failure.NETWORK, MESSAGES[Failure.NETWORK])
        headers = {"Referer": self._page_url} if self._page_url else {}
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
        try:
            if self._sent:
                await asyncio.sleep(PAUSE_SECONDS)
            self._sent += 1
            response = await self._client.request(
                method, self.url_for(path), headers=headers, data=data
            )
        except Exception as exc:
            raise EclassError(Failure.NETWORK, MESSAGES[Failure.NETWORK]) from _hide(exc)
        if not ajax:
            # 화면을 옮긴 요청만 다음 Referer가 된다
            self._page_url = str(response.url)
        return response

    def _saved_cookies(self) -> httpx.Cookies:
        """지난번 로그인의 쿠키. 없거나 깨졌으면 빈 채로 시작해 다시 로그인한다."""
        jar = httpx.Cookies()
        path: Path = self.settings.session_file
        if not path.exists():
            return jar
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.info("eClass 세션 파일을 읽지 못해 다시 로그인합니다")
            return jar
        for cookie in saved.get("cookies", []):
            try:
                jar.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain", ""),
                    path=cookie.get("path", "/"),
                )
            except (KeyError, TypeError):
                continue
        return jar

    def _save_cookies(self) -> None:
        """다음 수집이 다시 로그인하지 않도록 쿠키만 남긴다. 계정 정보는 담기지 않는다."""
        if self._client is None:
            return
        path: Path = self.settings.session_file
        cookies = [
            {"name": cookie.name, "value": cookie.value, "domain": cookie.domain, "path": cookie.path}
            for cookie in self._client.cookies.jar
        ]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"cookies": cookies}), encoding="utf-8")
        except OSError as exc:
            logger.info("eClass 세션을 저장하지 못했습니다 (%s)", type(exc).__name__)


def _hide(exc: Exception) -> None:
    """원래 예외에는 URL·입력값이 섞일 수 있어 연결하지 않고 종류만 남긴다."""
    logger.info("eClass 작업 실패: %s", type(exc).__name__)
    return None
