"""eClass 순수 HTTP 로그인 탐침 (docs/tasks.md T-21·T-22, ADR 0008).

서버가 8GB 맥북에어가 되어, 수집할 때마다 크로미움을 띄우지 않아도 되는지 확인한다.
브라우저 없이 httpx와 쿠키만으로 로그인하고 내용을 받을 수 있는지 **증거를 모으는** 도구다.
수집기는 바꾸지 않는다. 바꿀지는 이 결과를 보고 정한다 (T-23).

    uv run python -m scripts.eclass_http_probe                  # 1단계만. 계정을 쓰지 않는다
    uv run python -m scripts.eclass_http_probe --reuse-session  # 봇이 저장한 브라우저 세션으로 내용만 받아 본다
    uv run python -m scripts.eclass_http_probe --login          # 로그인을 한 번 시도하고 내용까지 받아 본다

권하는 순서: 1단계 → --reuse-session → (1단계가 "가공 없음"일 때만) --login.
앞의 둘은 로그인을 시도하지 않는다.

1단계 — 로그인 화면의 폼과 loginForm() 자바스크립트를 읽어, 비밀번호를 가공해 보내는지 판정한다.
내용 확인 — 봇이 쓰는 세 가지 요청 방식을 차례로 해 본다 (docs/refs/eclass-paths.md 1절).
  · 한 번에      받은 쪽지
  · 껍데기+내용  할 일. 봇의 할 일 수집과 같은 요청이고 X-Requested-With·Referer를 붙인다
  · 과목방      문을 열고(eclass_room2.acl) 강의계획서를 연다

**지켜야 할 선**
- 로그인은 한 번만 시도한다. 실패해도 다시 하지 않는다. 학교 계정은 연속 실패에 잠길 수 있다.
- 1단계가 "가공 있음"이나 "모름"이면 --login을 줘도 로그인하지 않는다 (--force로만 넘어간다).
  가공을 흉내 내지 않고 보내면 실패가 뻔하고, 실패는 잠금에 가까워지는 한 번이다.
- 조회만 한다. 정해 둔 주소만 열고, 탐색기와 같은 규칙으로 무언가를 바꾸는 주소를 막는다 (절대 규칙 5).
- 비밀번호는 로그인 요청 본문에만 들어간다. 화면·결과 파일·로그에 넣지 않는다 (절대 규칙 7).
- 학교 주소·쿠키는 찍지 않는다. 결과는 git에서 제외된 private/eclass_probe/에만 남긴다 (절대 규칙 12).
- 받은 화면의 글자는 데이터로만 다룬다 (절대 규칙 8).
"""

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from app.collectors.eclass.parse import parse_course_select, parse_todo_list
from app.collectors.eclass.session import (
    COURSE_ENTER_PATH,
    COURSE_ROOM_PATH,
    LOGIN_PATH,
    MAIN_PATH,
    TODO_PATH,
    classify_login,
    logged_in,
)
from app.collectors.eclass.sources.todo import TODO_FORM_DATA, TODO_ROWS_DATA, TODO_ROWS_PATH
from app.core.clock import KST
from app.core.config import PRIVATE_DIR, ConfigError, EclassSettings, load_settings
from scripts.eclass_explore import UNSAFE_WORDS, describe, safe_path

OUT_DIR = PRIVATE_DIR / "eclass_probe"

# 학교 서버에 몰아치지 않도록 요청 사이에 쉬는 시간(초). 탐색기와 같다.
PAUSE_SECONDS = 1.0
TIMEOUT_SECONDS = 20.0
# 로그인 화면이 부르는 바깥 스크립트는 이만큼만 받는다
MAX_SCRIPTS = 10

# 확인에 쓰는 화면. 봇이 이미 모으는 것들이라 새로 생기는 부작용이 없다.
MESSAGE_PATH = "/ilos/message/received_list_pop_form.acl"
SYLLABUS_PATH = "/ilos/st/course/plan_form.acl"

# 봇은 지금 크로미움으로 붙는다. 머리글이 달라 결과가 갈리지 않게 같은 계열로 맞춘다.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
# 로그인하지 않은 채 내용 주소를 부르면 오는 답 (docs/refs/eclass-paths.md 1절)
EXPIRED_MARKS = ("세션이 종료", "로그인 후 이용", "로그인이 필요")

# loginForm()이 비밀번호를 가공하는 흔적. 낱말 경계를 두어 "universal" 같은 말에 걸리지 않게 한다.
HINTS = {
    "해시": re.compile(r"sha-?(?:1|256|512)|hex_sha|md5|cryptojs|hmac", re.I),
    "암호화": re.compile(r"\brsa|jsencrypt|encrypt|setpublic|publickey", re.I),
    "인코딩": re.compile(r"\bbtoa\s*\(|base64", re.I),
}
# 비밀번호 칸이나 response 칸에 값을 써넣는 줄. 읽기(==)와 비우기(= "")는 걸리지 않는다.
_WRITES_SECRET = re.compile(
    r"(usr_pwd|response)[^;\n]{0,60}?(\.value\s*=(?!=)(?!\s*[\"']\s*[\"'])|\.val\(\s*[^)\s])", re.I
)
_CALL = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(")
# 함수 이름이 아닌데 뒤에 괄호가 오는 것들
_NOT_FUNCTIONS = frozenset({
    "if", "for", "while", "switch", "return", "function", "catch", "typeof", "new", "$",
    "alert", "confirm", "parseInt", "String", "Number", "encodeURIComponent", "setTimeout",
})
_META_CHARSET = re.compile(rb"<meta[^>]+charset=[\"']?([\w-]+)", re.I)


class Transform(StrEnum):
    NONE = "none"
    FOUND = "found"
    UNKNOWN = "unknown"


TRANSFORM_TEXT = {
    Transform.NONE: "가공 없음 — 폼에 적힌 그대로 보낸다",
    Transform.FOUND: "가공 있음 — 스크립트가 비밀번호나 response를 만든다",
    Transform.UNKNOWN: "모름 — loginForm()을 찾지 못했다",
}


class ProbeError(Exception):
    """탐침을 멈춘 이유. 원래 예외에는 학교 주소가 섞일 수 있어 종류만 담는다."""


@dataclass(slots=True)
class LoginPage:
    """로그인 화면을 읽은 결과."""

    action: str = ""
    same_site: bool = True
    id_field: str = ""
    password_field: str = ""
    # 이름 → 화면이 채워 둔 값. 제출에만 쓰고 결과 파일에는 이름만 남긴다.
    fields: dict[str, str] = field(default_factory=dict)
    hidden: list[str] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)
    login_js: str = ""
    transform: Transform = Transform.UNKNOWN
    hints: list[str] = field(default_factory=list)
    # 불러오는 파일 이름에만 걸린 흔적. loginForm()이 실제로 쓰는지는 모른다.
    library_hints: list[str] = field(default_factory=list)
    captcha: bool = False
    action_url: str = ""


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    path: str
    ok: bool
    items: int = 0
    note: str = ""


@dataclass(slots=True)
class Report:
    mode: str
    checked_at: datetime
    page: LoginPage = field(default_factory=LoginPage)
    login_page_html: str = ""
    login_tried: bool = False
    login_ok: bool | None = None
    login_note: str = ""
    session_ok: bool | None = None
    checks: list[Check] = field(default_factory=list)
    error: str = ""
    peak_rss_mb: float | None = None
    seconds: float = 0.0
    requests: list[str] = field(default_factory=list)
    encodings: list[str] = field(default_factory=list)

    @property
    def contents_ok(self) -> bool:
        return bool(self.checks) and all(check.ok for check in self.checks)

    def verdict(self) -> str:
        if self.error:
            return f"중간에 멈췄다 ({self.error}). 네트워크를 확인하고 다시 해 본다."
        if self.page.captcha:
            return "로그인 화면에 추가 인증이 걸려 있다. 브라우저로 직접 한 번 로그인해 풀고 나서 다시 한다."
        if self.mode == "inspect":
            follow = {
                Transform.NONE: "다음: --reuse-session, 그다음 --login",
                Transform.FOUND: "다음: --reuse-session으로 내용만 확인하고, login_form.js를 보고 계산을 옮긴다",
                Transform.UNKNOWN: "다음: login_page.html을 보고 로그인 단추가 무엇을 부르는지 찾는다",
            }[self.page.transform]
            return f"{TRANSFORM_TEXT[self.page.transform]}. {follow}"
        if self.mode == "reuse-session":
            if not self.session_ok:
                return "저장된 브라우저 세션이 만료됐다. 봇이 한 번 수집한 직후에 다시 해 본다."
            if self.contents_ok:
                return "쿠키만 있으면 내용은 HTTP로 받아진다 → 적어도 T-23 (나)는 된다. 로그인은 --login으로 따로 본다."
            return f"쿠키가 있어도 내용이 안 온다 ({self._failed()}) → T-23 (다) 쪽이다."
        # login
        if not self.login_tried:
            return f"로그인하지 않았다: {self.login_note}"
        if not self.login_ok:
            return (
                f"HTTP 로그인이 안 됐다 ({self.login_note}). **다시 시도하지 않는다.** "
                "loginForm()을 다시 보고, 안 되면 T-23 (나) — 로그인만 브라우저로."
            )
        if self.contents_ok:
            return "로그인과 내용 모두 HTTP로 된다 → T-23 (가). 크로미움을 걷어낼 수 있다."
        return f"로그인은 되지만 내용이 일부 안 온다 ({self._failed()}) → 실패한 요청 방식을 본다."

    def _failed(self) -> str:
        return ", ".join(check.name for check in self.checks if not check.ok) or "확인한 화면 없음"

    def to_dict(self) -> dict:
        """결과 파일에 남길 것. 폼 값·쿠키·학교 주소는 넣지 않는다."""
        page = self.page
        return {
            "checked_at": self.checked_at.isoformat(),
            "mode": self.mode,
            "login_page": {
                "action": page.action,
                "same_site": page.same_site,
                "fields": list(page.fields),
                "hidden": page.hidden,
                "id_field": page.id_field,
                "password_field": page.password_field,
                "scripts": page.scripts,
                "transform": str(page.transform),
                "hints": page.hints,
                "library_hints": page.library_hints,
                "captcha": page.captcha,
            },
            "login": {"tried": self.login_tried, "ok": self.login_ok, "note": self.login_note},
            "session_ok": self.session_ok,
            "checks": [
                {"name": c.name, "path": c.path, "ok": c.ok, "items": c.items, "note": c.note} for c in self.checks
            ],
            "error": self.error,
            "peak_rss_mb": self.peak_rss_mb,
            "seconds": self.seconds,
            "requests": self.requests,
            "encodings": self.encodings,
            "verdict": self.verdict(),
        }


# --- 1단계: 로그인 화면 읽기 ---


def function_body(script: str, name: str) -> str | None:
    """스크립트에서 이름이 name인 함수 전체를 꺼낸다. 없으면 None."""
    start = re.search(
        rf"function\s+{re.escape(name)}\s*\(|\b{re.escape(name)}\s*[:=]\s*function\s*\(", script
    )
    if not start:
        return None
    opening = script.find("{", start.end())
    if opening < 0:
        return None
    depth = 0
    for index in range(opening, len(script)):
        if script[index] == "{":
            depth += 1
        elif script[index] == "}":
            depth -= 1
            if depth == 0:
                return script[start.start() : index + 1]
    return script[start.start() :]  # 닫히지 않으면 끝까지


def callees(body: str, own: str) -> list[str]:
    """함수 안에서 부르는 다른 함수 이름. 메서드 호출(a.b())은 빼고 이름만 부르는 것만."""
    names = dict.fromkeys(match.group(1) for match in _CALL.finditer(body))
    return [name for name in names if name != own and name not in _NOT_FUNCTIONS]


def captcha_shown(soup: BeautifulSoup) -> bool:
    """추가 인증이 실제로 걸렸는지.

    로그인 화면에는 평소에도 숨은 reCaptcha 칸(capform)이 있다 (docs/refs/eclass-cli.md).
    낱말만 보면 늘 걸리므로, 걸렸을 때만 나타나는 스크립트·위젯을 본다.
    """
    if soup.select_one(".g-recaptcha, iframe[src*=recaptcha]"):
        return True
    return any("recaptcha" in (script.get("src") or "").lower() for script in soup.find_all("script"))


def analyze_login_page(html: str, page_url: str, external: dict[str, str] | None = None) -> LoginPage:
    """로그인 화면과 그 화면이 부르는 스크립트를 읽어 폼 구조와 가공 여부를 판정한다."""
    soup = BeautifulSoup(html, "html.parser")
    page = LoginPage(captcha=captcha_shown(soup))

    sources = [script.get("src") for script in soup.find_all("script") if script.get("src")]
    page.scripts = [path for src in sources if (path := _same_site_script(src, page_url))]
    page.library_hints = sorted({src for src in sources if any(rx.search(src) for rx in HINTS.values())})

    form = soup.find("form", attrs={"name": "myform"})
    if form is None:
        id_input = soup.find("input", id="usr_id") or soup.find("input", attrs={"name": "usr_id"})
        form = id_input.find_parent("form") if id_input else None
    if form is not None:
        _read_form(form, page, page_url)

    inline = [script.get_text() for script in soup.find_all("script") if not script.get("src")]
    corpus = "\n".join([*inline, *(external or {}).values()])
    body = function_body(corpus, "loginForm")
    if body is None:
        return page

    parts = [body] + [found for name in callees(body, "loginForm") if (found := function_body(corpus, name))]
    page.login_js = "\n\n".join(parts)
    hints = sorted({f"{label}: {m.group(0)}" for label, rx in HINTS.items() for m in rx.finditer(page.login_js)})
    if _WRITES_SECRET.search(page.login_js):
        hints.append("비밀번호·response 칸에 값을 써넣는다")
    page.hints = hints
    page.transform = Transform.FOUND if hints else Transform.NONE
    return page


def _read_form(form, page: LoginPage, page_url: str) -> None:
    action_url = urljoin(page_url, form.get("action") or LOGIN_PATH)
    page.action_url = action_url
    page.action = urlsplit(action_url).path
    page.same_site = urlsplit(action_url).netloc == urlsplit(page_url).netloc

    for node in form.find_all(["input", "select", "textarea"]):
        name = node.get("name")
        kind = (node.get("type") or "text").lower()
        if not name or kind in ("submit", "button", "image", "reset", "file"):
            continue
        if kind in ("checkbox", "radio") and not node.has_attr("checked"):
            continue
        page.fields[name] = node.get("value", "")
        if kind == "hidden":
            page.hidden.append(name)
        if node.get("id") == "usr_id":
            page.id_field = name
        if kind == "password" or node.get("id") == "usr_pwd":
            page.password_field = name
    page.id_field = page.id_field or ("usr_id" if "usr_id" in page.fields else "")
    page.password_field = page.password_field or ("usr_pwd" if "usr_pwd" in page.fields else "")


def _same_site_script(src: str, page_url: str) -> str | None:
    full = urljoin(page_url, src)
    parts = urlsplit(full)
    if parts.netloc != urlsplit(page_url).netloc or not parts.path.lower().endswith(".js"):
        return None
    return parts.path


def login_refusal(page: LoginPage, force: bool) -> str:
    """로그인을 시도하면 안 되는 이유. 시도해도 되면 빈 문자열."""
    if not page.action:
        return "로그인 폼을 찾지 못했다"
    if not page.same_site:
        return "로그인 폼이 다른 사이트로 보낸다 (포털 SSO로 보인다). 이 탐침은 따라가지 않는다"
    if not (page.id_field and page.password_field):
        return "아이디·비밀번호 칸을 찾지 못했다"
    if page.captcha:
        return "로그인 화면에 추가 인증이 걸려 있다"
    if page.transform is not Transform.NONE and not force:
        return (
            f"1단계 판정이 '{TRANSFORM_TEXT[page.transform]}'이라 로그인하지 않았다. "
            "private/eclass_probe/의 스크립트를 먼저 보고, 그래도 해 보려면 --force"
        )
    return ""


# --- 요청 ---


class Probe:
    """정해 둔 주소만 여는 httpx 세션. 브라우저처럼 직전 화면을 Referer로 붙인다."""

    def __init__(self, client: httpx.AsyncClient, base_url: str, pause: float) -> None:
        self._client = client
        self._base = base_url
        self._pause = pause
        self._page_url = ""
        self._sent = 0
        self.log: list[str] = []
        self.encodings: set[str] = set()

    def url(self, path: str) -> str:
        return urljoin(self._base, path)

    async def get(self, path: str, *, static: bool = False) -> tuple[str, int, str]:
        """화면을 연다. 화면을 옮기는 요청이라 다음 요청의 Referer가 된다 (바깥 스크립트는 빼고)."""
        _guard(path, static=static)
        response = await self._send("GET", path, headers=self._referer())
        if not static:
            self._page_url = str(response.url)
        return str(response.url), response.status_code, self._decode(response)

    async def post(self, path: str, data: dict[str, str], *, ajax: bool) -> tuple[str, int, str]:
        """ajax=True면 화면 안의 fetch처럼 보낸다. 봇의 session.post()와 같은 머리글이다."""
        _guard(path, static=False)
        headers = self._referer()
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
        response = await self._send("POST", path, headers=headers, data=data)
        if not ajax:
            self._page_url = str(response.url)
        return str(response.url), response.status_code, self._decode(response)

    def _referer(self) -> dict[str, str]:
        return {"Referer": self._page_url} if self._page_url else {}

    async def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        if self._sent:
            await asyncio.sleep(self._pause)
        self._sent += 1
        try:
            response = await self._client.request(method, self.url(path), **kwargs)
        except httpx.HTTPError as exc:
            # 예외 문구에는 학교 주소가 들어 있다. 종류만 남긴다.
            self.log.append(f"{method} {path} → {type(exc).__name__}")
            raise ProbeError(f"{method} {path}에서 {type(exc).__name__}") from None
        self.log.append(f"{method} {path} → {response.status_code}")
        return response

    def _decode(self, response: httpx.Response) -> str:
        """머리글에 문자셋이 없으면 화면의 meta를 본다. 한글 화면을 utf-8로 잘못 읽지 않게."""
        encoding = response.charset_encoding
        if not encoding:
            found = _META_CHARSET.search(response.content[:4096])
            encoding = found.group(1).decode("ascii") if found else "utf-8"
        self.encodings.add(encoding.lower())
        return response.content.decode(encoding, errors="replace")


def _guard(path: str, *, static: bool) -> None:
    """조회 화면만 연다 (탐색기와 같은 규칙). 바깥 스크립트는 .js 파일만."""
    lowered = path.lower()
    if static:
        allowed = lowered.endswith(".js") and not any(word in lowered for word in UNSAFE_WORDS)
    else:
        allowed = safe_path(path)
    if not allowed:
        raise ProbeError(f"열지 않기로 한 주소라 멈췄다: {path}")


def expired(url: str, html: str) -> bool:
    return LOGIN_PATH in url or any(mark in html for mark in EXPIRED_MARKS)


async def inspect_login(probe: Probe) -> tuple[LoginPage, str]:
    page_url, _status, html = await probe.get(LOGIN_PATH)
    first = analyze_login_page(html, page_url)
    external = {}
    for path in first.scripts[:MAX_SCRIPTS]:
        _url, status, text = await probe.get(path, static=True)
        if status < 400:
            external[path] = text
    return analyze_login_page(html, page_url, external), html


async def login(probe: Probe, page: LoginPage, settings: EclassSettings) -> str:
    """폼대로 한 번 로그인한다. 성공이면 빈 문자열, 실패면 사유."""
    data = dict(page.fields)
    data[page.id_field] = settings.username
    # 비밀번호는 이 요청 본문에만 들어간다
    data[page.password_field] = settings.password
    await probe.post(page.action, data, ajax=False)
    url, _status, html = await probe.get(MAIN_PATH)
    reason = classify_login(url, html)
    return str(reason) if reason is not None else ""


async def check_contents(probe: Probe) -> list[Check]:
    """봇이 쓰는 세 가지 요청 방식을 차례로 해 본다."""
    checks: list[Check] = []

    url, status, html = await probe.get(MESSAGE_PATH)
    checks.append(_screen_check("한 번에 (받은 쪽지)", MESSAGE_PATH, url, status, html))

    # 할 일: 봇의 할 일 수집과 같은 순서 (메인 → 껍데기 → 내용)
    await probe.get(MAIN_PATH)
    _url, _status, form_html = await probe.post(TODO_PATH, dict(TODO_FORM_DATA), ajax=True)
    url, status, rows_html = await probe.post(TODO_ROWS_PATH, dict(TODO_ROWS_DATA), ajax=True)
    checks.append(_todo_check(url, status, rows_html))

    keys = [row.kjkey for row in parse_course_select(form_html).rows]
    if not keys:
        checks.append(
            Check("과목방 (강의계획서)", SYLLABUS_PATH, False, note="과목 열쇠를 찾지 못했다 (할 일 껍데기를 못 받았다)")
        )
        return checks
    # 문을 열 뿐 아무것도 바꾸지 않는다. 봇의 enter_course()와 같은 요청이다.
    await probe.post(
        COURSE_ENTER_PATH,
        {"KJKEY": keys[0], "returnData": "json", "returnURI": COURSE_ROOM_PATH, "encoding": "utf-8"},
        ajax=True,
    )
    await probe.get(COURSE_ROOM_PATH)
    url, status, html = await probe.get(SYLLABUS_PATH)
    checks.append(_screen_check("과목방 (강의계획서)", SYLLABUS_PATH, url, status, html))
    return checks


def _screen_check(name: str, path: str, url: str, status: int, html: str) -> Check:
    if status >= 400:
        return Check(name, path, False, note=f"HTTP {status}")
    if expired(url, html):
        return Check(name, path, False, note="세션이 끊겼다는 답")
    items = describe(html).items
    return Check(name, path, items > 0, items, "" if items else "줄이 없다 (비어 있거나 구조가 다르다)")


def _todo_check(url: str, status: int, html: str) -> Check:
    name = "껍데기+내용 (할 일)"
    if status >= 400:
        return Check(name, TODO_ROWS_PATH, False, note=f"HTTP {status}")
    if expired(url, html):
        return Check(name, TODO_ROWS_PATH, False, note="세션이 끊겼다는 답")
    result = parse_todo_list(html)
    if result.suspicious:
        return Check(name, TODO_ROWS_PATH, False, note="구조가 달라 읽지 못했다")
    return Check(name, TODO_ROWS_PATH, True, len(result.rows), "" if result.rows else "할 일이 비어 있다")


# --- 실행 ---


def load_browser_cookies(path: Path) -> httpx.Cookies:
    """봇의 Playwright 세션 파일(storage_state)에서 쿠키만 꺼낸다. 값은 어디에도 찍지 않는다."""
    state = json.loads(path.read_text(encoding="utf-8"))
    jar = httpx.Cookies()
    for cookie in state.get("cookies", []):
        jar.set(cookie["name"], cookie["value"], domain=cookie.get("domain", ""), path=cookie.get("path", "/"))
    return jar


def peak_rss_mb() -> float | None:
    """이 프로세스가 지금까지 가장 많이 쓴 메모리(MB). 파이썬과 불러온 모듈까지 다 든 값이다."""
    try:
        import resource
    except ImportError:  # 윈도우에는 없다. 서버(맥북에어)에서 다시 잰다.
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS는 바이트, 리눅스는 킬로바이트로 준다
    return round(peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024, 1)


def _client(transport: httpx.AsyncBaseTransport | None, cookies: httpx.Cookies | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=transport,
        cookies=cookies,
        follow_redirects=True,
        timeout=TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"},
    )


async def run_probe(
    settings: EclassSettings,
    *,
    mode: str,
    force: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    pause: float = PAUSE_SECONDS,
    now: datetime | None = None,
) -> Report:
    # httpx는 INFO에 요청 주소를 통째로 찍는다. 학교 주소가 로그에 남지 않게 한다 (app/main.py와 같은 규칙).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    started = time.perf_counter()
    report = Report(mode=mode, checked_at=now or datetime.now(KST))
    probes: list[Probe] = []
    try:
        # 1단계와 로그인은 같은 세션이어야 한다 (challenge 값이 그 세션에 묶여 있을 수 있다)
        async with _client(transport) as client:
            probe = Probe(client, settings.eclass_url, pause)
            probes.append(probe)
            report.page, report.login_page_html = await inspect_login(probe)
            if mode == "login":
                report.login_note = login_refusal(report.page, force)
                if not report.login_note:
                    report.login_tried = True
                    report.login_note = await login(probe, report.page, settings)
                    report.login_ok = not report.login_note
                    if report.login_ok:
                        report.checks = await check_contents(probe)

        if mode == "reuse-session":
            cookies = load_browser_cookies(settings.session_file)
            async with _client(transport, cookies) as client:
                probe = Probe(client, settings.eclass_url, pause)
                probes.append(probe)
                _url, _status, html = await probe.get(MAIN_PATH)
                report.session_ok = logged_in(html)
                if report.session_ok:
                    report.checks = await check_contents(probe)
    except ProbeError as exc:
        report.error = str(exc)

    report.requests = [line for probe in probes for line in probe.log]
    report.encodings = sorted({encoding for probe in probes for encoding in probe.encodings})
    report.seconds = round(time.perf_counter() - started, 1)
    report.peak_rss_mb = peak_rss_mb()
    return report


def write_report(report: Report, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    if report.login_page_html:
        (out_dir / "login_page.html").write_text(report.login_page_html, encoding="utf-8")
    if report.page.login_js:
        (out_dir / "login_form.js").write_text(report.page.login_js, encoding="utf-8")
    return out_dir


MODE_TITLE = {"inspect": "1단계만", "reuse-session": "저장된 세션으로", "login": "로그인까지"}


def render(report: Report) -> str:
    page = report.page
    lines = [
        f"eClass HTTP 탐침 ({MODE_TITLE[report.mode]})  {report.checked_at:%Y-%m-%d %H:%M}",
        "",
        "[1단계] 로그인 화면",
    ]
    if page.action:
        site = "같은 사이트" if page.same_site else "다른 사이트"
        lines.append(f"  폼: POST {page.action}  ({site})")
        lines.append(f"  칸: {', '.join(page.fields) or '없음'}  (숨은 칸: {', '.join(page.hidden) or '없음'})")
    else:
        lines.append("  폼: 찾지 못했다")
    lines += [
        f"  바깥 스크립트: {len(page.scripts)}개",
        f"  판정: {TRANSFORM_TEXT[page.transform]}",
        f"  걸린 낱말: {', '.join(page.hints) or '없음'}",
    ]
    if page.library_hints:
        lines.append(f"  (불러오는 파일 이름에만 걸림: {', '.join(page.library_hints)})")
    lines.append(f"  추가 인증: {'걸려 있다' if page.captcha else '없음'}")

    if report.mode == "login":
        lines += ["", "[로그인]"]
        if not report.login_tried:
            lines.append(f"  시도하지 않았다 — {report.login_note}")
        else:
            lines.append("  성공" if report.login_ok else f"  실패 — {report.login_note} (다시 시도하지 않는다)")
    if report.mode == "reuse-session":
        lines += ["", "[저장된 세션]", "  살아 있다" if report.session_ok else "  만료됐다"]

    if report.checks:
        lines += ["", "[내용]"]
        for check in report.checks:
            mark = "○" if check.ok else "×"
            detail = f"{check.items}줄" if check.ok else check.note
            lines.append(f"  {mark} {check.name:<18} {check.path}  {detail}")

    memory = f"{report.peak_rss_mb}MB" if report.peak_rss_mb is not None else "재지 못함 (이 OS에서는 안 된다)"
    lines += [
        "",
        f"메모리(최대 RSS, 탐침 프로세스 전체): {memory} · 걸린 시간 {report.seconds}초 · 요청 {len(report.requests)}번",
        f"문자셋: {', '.join(report.encodings) or '없음'}",
        "",
        f"판정: {report.verdict()}",
    ]
    return "\n".join(lines)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="eClass 순수 HTTP 로그인 탐침 (docs/tasks.md T-21)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--reuse-session", action="store_true", help="봇이 저장한 브라우저 세션의 쿠키로 내용만 받아 본다 (로그인 안 함)"
    )
    mode.add_argument("--login", action="store_true", help="로그인을 한 번 시도하고 내용까지 받아 본다")
    parser.add_argument("--force", action="store_true", help="1단계 판정이 '가공 없음'이 아니어도 로그인을 시도한다")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    # 윈도우 콘솔 기본 인코딩(cp949)으로는 한글 밖의 기호에서 멈춘다
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    try:
        settings = load_settings().eclass
    except ConfigError as exc:
        print(f"설정을 읽지 못했습니다: {exc}")
        return 1
    if not settings.eclass_url:
        print("eClass 주소가 없습니다. private/local.toml의 [eclass] eclass_url을 확인해 주세요.")
        return 1

    mode = "login" if args.login else "reuse-session" if args.reuse_session else "inspect"
    if mode == "login" and not (settings.username and settings.password):
        print("학교 계정이 없습니다. private/.env의 ECLASS_ID·ECLASS_PASSWORD를 확인해 주세요.")
        return 1
    if mode == "reuse-session" and not settings.session_file.exists():
        print("저장된 브라우저 세션이 없습니다. 봇이 eClass를 한 번 수집한 뒤에 다시 해 주세요.")
        return 1

    report = await run_probe(settings, mode=mode, force=args.force)
    out = write_report(report, OUT_DIR)
    print(render(report))
    print(f"\n결과: {out}")
    return 0 if not report.error else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
