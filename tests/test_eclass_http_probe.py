"""eClass HTTP 탐침 (scripts/eclass_http_probe.py) 확인.

실제 사이트에 붙지 않는다. httpx.MockTransport로 만든 가짜 eClass에서 판정과 안전 규칙만 본다.
실제 실행은 private/가 있는 기기에서 한다 (docs/tasks.md T-22).
"""

import json
import logging
from urllib.parse import parse_qs

import httpx
import pytest

from app.core.config import EclassSettings
from scripts import eclass_http_probe as probe_mod
from scripts.eclass_explore import UNSAFE_WORDS, safe_path
from scripts.eclass_http_probe import (
    Transform,
    analyze_login_page,
    callees,
    function_body,
    login_refusal,
    peak_rss_mb,
    render,
    run_probe,
    write_report,
)
from tests.conftest import kst

BASE = "https://eclass.example.ac.kr/"
LOGIN_URL = BASE + "ilos/main/member/login_form.acl"
PASSWORD = "pw-SECRET-1234"
SETTINGS = EclassSettings(eclass_url=BASE, username="학번", password=PASSWORD, poll_minutes=90)
NOW = kst(9, 21, 16)
SESSION_COOKIE = "ECLASS_SESSION"

PLAIN_JS = """
function loginForm() {
    if (document.myform.usr_id.value == "") { alert("아이디를 넣어 주세요"); return; }
    if (document.myform.usr_pwd.value == "") { alert("비밀번호를 넣어 주세요"); return; }
    document.myform.submit();
}
"""
HASHING_JS = """
function loginForm() {
    var f = document.myform;
    f.response.value = hex_sha256(f.challenge.value + f.usr_pwd.value);
    f.usr_pwd.value = "";
    f.submit();
}
"""
# 가공은 loginForm()이 부르는 다른 함수 안에 있다
RSA_CALLEE_JS = """
function loginForm() { encodePassword(); document.myform.submit(); }
function encodePassword() {
    var key = new RSAKey();
    key.setPublic(modulus, exponent);
    document.myform.usr_pwd.value = key.encrypt(document.myform.usr_pwd.value);
}
"""


def login_page(script: str = PLAIN_JS, *, external: str = "", action: str = "/ilos/lo/login.acl", captcha: bool = False) -> str:
    head = f'<script src="{external}"></script>' if external else ""
    if captcha:
        head += '<script src="https://www.google.com/recaptcha/api.js"></script>'
    inline = f"<script>{script}</script>" if script else ""
    return f"""<html><head>{head}{inline}</head><body>
    <form name="myform" method="post" action="{action}">
      <input type="text" id="usr_id" name="usr_id">
      <input type="password" id="usr_pwd" name="usr_pwd">
      <input type="hidden" name="returnURL" value="">
      <input type="hidden" name="challenge" value="c-123">
      <input type="hidden" name="response" value="">
      <div onclick="loginForm();">로그인</div>
    </form>
    <form name="capform"><input type="hidden" name="reCaptcha" value=""></form>
    </body></html>"""


TODO_FORM = """<select id="todo_select">
  <option value="">전체 과목 보기</option>
  <option value="KJ1||L">[2026년 2학기] 자료구조</option>
</select>"""
TODO_ROWS = """<div class="todo_list">
  <div class="todo_wrap" onclick="goLecture('KJ1','7','report')">
    <div class="todo_subjt">자료구조</div><div class="todo_title">과제 1</div>
    <div class="todo_date">2026.09.25 오후 11:59</div>
  </div>
</div>"""
TABLE = "<table><tr><td>1</td><td>첫째</td></tr><tr><td>2</td><td>둘째</td></tr></table>"
SYLLABUS = '<table class="bbsview"><tr><th>교과목명</th><td>자료구조</td><th>학점</th><td>3</td></tr></table>'
EXPIRED = "<script>alert('세션이 종료되었습니다.');</script>"


class FakeEclass:
    """가짜 eClass. 로그인하면 쿠키를 주고, 쿠키가 있어야 내용을 준다."""

    def __init__(self, page: str = "", *, external_js: str = "", block_contents: bool = False) -> None:
        self.page = page or login_page()
        self.external_js = external_js
        self.block_contents = block_contents
        self.requests: list[httpx.Request] = []
        self.room = ""

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def authed(self, request: httpx.Request) -> bool:
        return f"{SESSION_COOKIE}=ok" in request.headers.get("cookie", "")

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if path == "/ilos/main/member/login_form.acl":
            return html(self.page)
        if path == "/ilos/js/login.js":
            return httpx.Response(200, text=self.external_js, headers={"content-type": "application/javascript"})
        if path == "/ilos/lo/login.acl" and method == "POST":
            form = parse_qs(request.content.decode())
            ok = form.get("usr_id") == ["학번"] and form.get("usr_pwd") == [PASSWORD]
            # 실제 사이트처럼 실패해도 메인으로 보낸다 (docs/refs/eclass-paths.md 2절)
            headers = {"location": "/ilos/main/main_form.acl"}
            if ok:
                headers["set-cookie"] = f"{SESSION_COOKIE}=ok; Path=/"
            return httpx.Response(302, headers=headers)
        if path == "/ilos/main/main_form.acl":
            if self.authed(request):
                return html('<a href="/ilos/lo/logout.acl">로그아웃</a>')
            return html('<input id="usr_id" name="usr_id">')

        if not self.authed(request) or self.block_contents:
            return html(EXPIRED)
        if path == "/ilos/mp/todo_list_form.acl":
            return html(TODO_FORM)
        if path == "/ilos/mp/todo_list.acl":
            # 화면 안의 fetch처럼 보내야만 내용을 준다
            if request.headers.get("x-requested-with") != "XMLHttpRequest":
                return html(EXPIRED)
            return html(TODO_ROWS)
        if path == "/ilos/message/received_list_pop_form.acl":
            return html(TABLE)
        if path == "/ilos/st/course/eclass_room2.acl":
            self.room = parse_qs(request.content.decode()).get("KJKEY", [""])[0]
            return httpx.Response(200, json={"isError": False})
        if path == "/ilos/st/course/submain_form.acl":
            return html("<div>과목방</div>")
        if path == "/ilos/st/course/plan_form.acl":
            return html(SYLLABUS if self.room else "<div>과목을 먼저 고르세요</div>")
        return httpx.Response(404)

    def paths(self, method: str | None = None) -> list[str]:
        return [r.url.path for r in self.requests if method is None or r.method == method]


def html(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})


async def probe(site: FakeEclass, mode: str, **kwargs):
    return await run_probe(SETTINGS, mode=mode, transport=site.transport(), pause=0, now=NOW, **kwargs)


# --- 1단계: 로그인 화면 판정 ---


def test_plain_login_form_is_read_as_is():
    page = analyze_login_page(login_page(PLAIN_JS), LOGIN_URL)
    assert page.action == "/ilos/lo/login.acl" and page.same_site
    assert list(page.fields) == ["usr_id", "usr_pwd", "returnURL", "challenge", "response"]
    assert page.hidden == ["returnURL", "challenge", "response"]
    assert (page.id_field, page.password_field) == ("usr_id", "usr_pwd")
    assert page.transform is Transform.NONE and page.hints == []


def test_hashing_the_password_is_found():
    page = analyze_login_page(login_page(HASHING_JS), LOGIN_URL)
    assert page.transform is Transform.FOUND
    assert any(hint.startswith("해시") for hint in page.hints)
    assert "비밀번호·response 칸에 값을 써넣는다" in page.hints


def test_encryption_inside_a_called_function_is_found():
    page = analyze_login_page(login_page(RSA_CALLEE_JS), LOGIN_URL)
    assert page.transform is Transform.FOUND
    assert "encodePassword" in page.login_js
    assert any(hint.startswith("암호화") for hint in page.hints)


def test_missing_login_function_is_unknown():
    page = analyze_login_page(login_page(script=""), LOGIN_URL)
    assert page.transform is Transform.UNKNOWN and page.login_js == ""


def test_hidden_recaptcha_field_alone_is_not_a_captcha():
    """로그인 화면에는 평소에도 숨은 reCaptcha 칸이 있다. 그것만으로 추가 인증이라 하지 않는다."""
    assert not analyze_login_page(login_page(), LOGIN_URL).captcha
    assert analyze_login_page(login_page(captcha=True), LOGIN_URL).captcha


def test_words_inside_other_words_are_not_hints():
    page = analyze_login_page(login_page("function loginForm() { universal(); document.myform.submit(); }"), LOGIN_URL)
    assert page.transform is Transform.NONE


def test_function_body_and_callees():
    script = "var a = 1; function loginForm() { if (x) { check(); } go(); $.ajax(); obj.run(); }"
    body = function_body(script, "loginForm")
    assert body.startswith("function loginForm") and body.endswith("}")
    assert callees(body, "loginForm") == ["check", "go"]


async def test_login_function_in_an_outside_script_is_fetched():
    site = FakeEclass(login_page(script="", external="/ilos/js/login.js"), external_js=HASHING_JS)
    report = await probe(site, "inspect")
    assert "/ilos/js/login.js" in site.paths("GET")
    assert report.page.transform is Transform.FOUND


# --- 로그인 여부 ---


def test_login_is_refused_when_the_first_step_is_not_clean():
    hashed = analyze_login_page(login_page(HASHING_JS), LOGIN_URL)
    assert "로그인하지 않았다" in login_refusal(hashed, force=False)
    assert login_refusal(hashed, force=True) == ""

    unknown = analyze_login_page(login_page(script=""), LOGIN_URL)
    assert login_refusal(unknown, force=False)


def test_login_to_another_site_is_refused_even_with_force():
    page = analyze_login_page(login_page(action="https://portal.example.ac.kr/sso/login"), LOGIN_URL)
    assert not page.same_site
    assert "다른 사이트" in login_refusal(page, force=True)


async def test_refused_login_sends_no_password():
    site = FakeEclass(login_page(HASHING_JS))
    report = await probe(site, "login")
    assert not report.login_tried
    assert "/ilos/lo/login.acl" not in site.paths()


# --- 로그인과 내용 확인 ---


async def test_plain_login_and_all_three_request_styles_work():
    site = FakeEclass()
    report = await probe(site, "login")

    assert report.login_tried and report.login_ok
    assert [(c.name, c.ok, c.items) for c in report.checks] == [
        ("한 번에 (받은 쪽지)", True, 2),
        ("껍데기+내용 (할 일)", True, 1),
        ("과목방 (강의계획서)", True, 1),
    ]
    assert site.room == "KJ1"
    assert "T-23 (가)" in report.verdict()


async def test_failed_login_is_tried_only_once_and_stops():
    site = FakeEclass()
    wrong = EclassSettings(eclass_url=BASE, username="학번", password="틀린 비밀번호", poll_minutes=90)
    report = await run_probe(wrong, mode="login", transport=site.transport(), pause=0, now=NOW)

    assert report.login_tried and not report.login_ok
    assert report.login_note == "login"
    assert site.paths("POST").count("/ilos/lo/login.acl") == 1
    assert report.checks == []
    assert "다시 시도하지 않는다" in report.verdict()


async def test_contents_refused_by_the_server_are_reported():
    site = FakeEclass(block_contents=True)
    report = await probe(site, "login")
    assert report.login_ok
    assert not report.contents_ok
    assert {c.note for c in report.checks if not c.ok} >= {"세션이 끊겼다는 답"}
    assert "일부 안 온다" in report.verdict()


async def test_ajax_requests_carry_the_same_headers_as_the_bot():
    site = FakeEclass()
    await probe(site, "login")
    rows = next(r for r in site.requests if r.url.path == "/ilos/mp/todo_list.acl")
    assert rows.headers["x-requested-with"] == "XMLHttpRequest"
    assert rows.headers["referer"].endswith("/ilos/main/main_form.acl")


# --- 저장된 브라우저 세션 재사용 (로그인 없음) ---


def storage_state(tmp_path, value: str = "ok"):
    path = tmp_path / "eclass_session.json"
    cookie = {"name": SESSION_COOKIE, "value": value, "domain": "eclass.example.ac.kr", "path": "/"}
    path.write_text(json.dumps({"cookies": [cookie], "origins": []}), encoding="utf-8")
    return path


async def test_reused_browser_session_checks_contents_without_logging_in(tmp_path):
    site = FakeEclass()
    settings = EclassSettings(eclass_url=BASE, session_file=storage_state(tmp_path))
    report = await run_probe(settings, mode="reuse-session", transport=site.transport(), pause=0, now=NOW)

    assert "/ilos/lo/login.acl" not in site.paths()
    assert report.session_ok and report.contents_ok
    assert "T-23 (나)" in report.verdict()


async def test_expired_browser_session_is_reported(tmp_path):
    site = FakeEclass()
    settings = EclassSettings(eclass_url=BASE, session_file=storage_state(tmp_path, value="old"))
    report = await run_probe(settings, mode="reuse-session", transport=site.transport(), pause=0, now=NOW)
    assert report.session_ok is False and report.checks == []
    assert "만료" in report.verdict()


# --- 안전 규칙 ---


async def test_only_reading_screens_are_opened():
    site = FakeEclass(login_page(external="/ilos/js/login.js"), external_js=PLAIN_JS)
    await probe(site, "login")
    for path in site.paths():
        assert safe_path(path) or path.endswith(".js"), path
        assert not any(word in path.lower() for word in UNSAFE_WORDS), path


async def test_network_error_stops_without_leaking_the_address():
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("could not reach eclass.example.ac.kr", request=request)

    report = await run_probe(SETTINGS, mode="inspect", transport=httpx.MockTransport(broken), pause=0, now=NOW)
    assert "ConnectError" in report.error
    assert "example.ac.kr" not in report.error and "example.ac.kr" not in render(report)


async def test_password_never_leaves_the_login_request(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    site = FakeEclass()
    report = await probe(site, "login")
    out = write_report(report, tmp_path / "probe")

    written = "".join(path.read_text(encoding="utf-8") for path in out.iterdir())
    for text in (render(report), json.dumps(report.to_dict(), ensure_ascii=False), written, caplog.text):
        assert PASSWORD not in text
        assert "eclass.example.ac.kr" not in text
    # 비밀번호가 실제로 나간 곳은 로그인 요청 하나뿐이다
    carrying = [r.url.path for r in site.requests if PASSWORD in r.content.decode(errors="ignore")]
    assert carrying == ["/ilos/lo/login.acl"]


def test_report_keeps_field_names_but_not_values():
    page = analyze_login_page(login_page(), LOGIN_URL)
    report = probe_mod.Report(mode="inspect", checked_at=NOW, page=page)
    saved = report.to_dict()["login_page"]
    assert saved["fields"] == ["usr_id", "usr_pwd", "returnURL", "challenge", "response"]
    assert "c-123" not in json.dumps(report.to_dict())


# --- 기타 ---


def test_peak_memory_is_measured_on_this_machine():
    value = peak_rss_mb()
    assert value is None or value > 0


def test_mode_options_are_exclusive():
    with pytest.raises(SystemExit):
        probe_mod.parse_args(["--login", "--reuse-session"])


async def test_main_stops_without_an_eclass_address(monkeypatch, capsys):
    monkeypatch.setattr(probe_mod, "load_settings", lambda: type("S", (), {"eclass": EclassSettings()})())
    assert await probe_mod.main([]) == 1
    assert "eclass_url" in capsys.readouterr().out
