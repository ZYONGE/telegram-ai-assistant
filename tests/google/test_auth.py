import asyncio
import base64
import hashlib
import json
import urllib.parse
from datetime import timedelta

import httpx
import pytest

from app.google.auth import (
    LOGIN_NEEDED,
    ClientSecrets,
    GoogleAuth,
    GoogleAuthError,
    StoredToken,
    TransientGoogleError,
    authorization_url,
    parse_redirect,
    pkce_pair,
)
from tests.conftest import Clock, kst

CLIENT_JSON = {
    "installed": {
        "client_id": "test-client-id.apps.googleusercontent.com",
        "client_secret": "test-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}


@pytest.fixture
def client_file(tmp_path):
    path = tmp_path / "google_client.json"
    path.write_text(json.dumps(CLIENT_JSON), encoding="utf-8")
    return path


@pytest.fixture
def token_file(tmp_path):
    return tmp_path / "google_token_1.json"


def auth_with(handler, client_file, token_file, clock=None):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GoogleAuth(client_file, token_file, http, clock=clock or Clock(kst(9, 18, 12))), http


def token_response(access="new-access", expires=3600, refresh=None):
    body = {"access_token": access, "expires_in": expires, "scope": "calendar gmail.modify"}
    if refresh:
        body["refresh_token"] = refresh
    return httpx.Response(200, json=body)


# --- 클라이언트 파일과 토큰 파일 ---


def test_client_secrets_reads_installed_or_web(tmp_path, client_file):
    secrets = ClientSecrets.load(client_file)
    assert secrets.client_id.endswith("googleusercontent.com") and secrets.client_secret == "test-secret"

    web = tmp_path / "web.json"
    web.write_text(json.dumps({"web": CLIENT_JSON["installed"]}), encoding="utf-8")
    assert ClientSecrets.load(web).client_id == secrets.client_id


@pytest.mark.parametrize(
    ("content", "message"),
    [(None, "클라이언트 파일이 없습니다"), ("{", "읽지 못했습니다"), ("{}", "형식이 다릅니다")],
)
def test_client_secrets_problems_explain_what_to_do(tmp_path, content, message):
    path = tmp_path / "client.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(GoogleAuthError, match=message):
        ClientSecrets.load(path)


def test_stored_token_round_trip_and_validity():
    token = StoredToken("refresh", "access", kst(9, 18, 13), ("calendar",))
    again = StoredToken.from_json(token.to_json())
    assert again == token
    assert again.is_valid(kst(9, 18, 12, 50)) is True
    # 만료 2분 전부터는 갱신한다
    assert again.is_valid(kst(9, 18, 12, 59)) is False
    assert StoredToken("refresh").is_valid(kst(9, 18, 12)) is False


# --- 토큰 갱신 ---


async def test_access_token_is_reused_until_it_nears_expiry(client_file, token_file):
    calls = []

    def handler(request):
        calls.append(request)
        return token_response()

    clock = Clock(kst(9, 18, 12))
    auth, http = auth_with(handler, client_file, token_file, clock)
    auth.save_token(StoredToken("refresh-1", "old-access", kst(9, 18, 12, 40)))
    async with http:
        assert await auth.access_token() == "old-access"
        assert calls == []
        # 만료 2분 전이 되면 미리 갱신한다
        clock.now = kst(9, 18, 12, 39)
        assert await auth.access_token() == "new-access"
    assert len(calls) == 1
    body = dict(urllib.parse.parse_qsl(calls[0].content.decode()))
    assert body["grant_type"] == "refresh_token" and body["refresh_token"] == "refresh-1"
    # 갱신 결과가 파일에 남아 다음 실행에서도 쓰인다
    assert StoredToken.from_json(token_file.read_text(encoding="utf-8")).access_token == "new-access"


async def test_refresh_keeps_the_old_refresh_token_when_google_omits_it(client_file, token_file):
    auth, http = auth_with(lambda request: token_response(), client_file, token_file)
    auth.save_token(StoredToken("refresh-1", "", None))
    async with http:
        await auth.access_token()
    assert StoredToken.from_json(token_file.read_text(encoding="utf-8")).refresh_token == "refresh-1"


async def test_missing_token_file_tells_the_user_to_log_in(client_file, token_file):
    auth, http = auth_with(lambda request: token_response(), client_file, token_file)
    async with http:
        with pytest.raises(GoogleAuthError, match="연결이 아직"):
            await auth.access_token()
    assert LOGIN_NEEDED.startswith("Google 계정 연결이 아직")


async def test_revoked_permission_asks_for_a_new_login(client_file, token_file):
    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    auth, http = auth_with(handler, client_file, token_file)
    auth.save_token(StoredToken("refresh-1"))
    async with http:
        with pytest.raises(GoogleAuthError, match="다시 해 주세요"):
            await auth.access_token()


@pytest.mark.parametrize(
    "response",
    [httpx.Response(503, text="down"), httpx.Response(500, text="oops")],
)
async def test_server_errors_are_transient(client_file, token_file, response):
    auth, http = auth_with(lambda request: response, client_file, token_file)
    auth.save_token(StoredToken("refresh-1"))
    async with http:
        with pytest.raises(TransientGoogleError):
            await auth.access_token()


async def test_secrets_are_never_in_error_messages(client_file, token_file):
    def handler(request):
        return httpx.Response(400, json={"error": "unauthorized_client", "error_description": "test-secret leaked"})

    auth, http = auth_with(handler, client_file, token_file)
    auth.save_token(StoredToken("refresh-secret-value"))
    async with http:
        with pytest.raises(GoogleAuthError) as error:
            await auth.access_token()
    assert "test-secret" not in str(error.value) and "refresh-secret-value" not in str(error.value)


# --- 로그인 흐름 (PKCE + 루프백) ---


def test_pkce_pair_is_sha256_of_the_verifier():
    verifier, challenge = pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected and "=" not in challenge


def test_authorization_url_asks_for_offline_access():
    url = authorization_url(ClientSecrets("id", "secret"), "http://127.0.0.1:1234/", "challenge", "state", ["scope-a"])
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert query["client_id"] == "id" and query["redirect_uri"] == "http://127.0.0.1:1234/"
    assert query["access_type"] == "offline" and query["prompt"] == "consent"
    assert query["code_challenge_method"] == "S256" and query["code_challenge"] == "challenge"
    assert query["scope"] == "scope-a" and query["state"] == "state"


def test_parse_redirect_reads_the_query():
    assert parse_redirect("GET /?code=abc&state=xyz HTTP/1.1") == {"code": "abc", "state": "xyz"}
    assert parse_redirect("GET / HTTP/1.1") == {}


async def visit(url: str) -> None:
    """브라우저가 리디렉션 주소를 여는 것을 흉내 낸다."""
    parsed = urllib.parse.urlparse(dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))["redirect_uri"])
    query = urllib.parse.urlparse(url).query
    params = dict(urllib.parse.parse_qsl(query))
    reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
    target = f"/?code=test-code&state={params['state']}"
    writer.write(f"GET {target} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
    await writer.drain()
    await reader.read(100)
    writer.close()


async def test_login_exchanges_the_code_and_saves_the_refresh_token(client_file, token_file):
    exchanges = []

    def handler(request):
        exchanges.append(dict(urllib.parse.parse_qsl(request.content.decode())))
        return token_response(refresh="refresh-new")

    auth, http = auth_with(handler, client_file, token_file)
    opened: list[str] = []

    def open_browser(url: str) -> None:
        opened.append(url)
        asyncio.get_running_loop().create_task(visit(url))

    async with http:
        token = await auth.login(open_browser)

    assert token.refresh_token == "refresh-new"
    assert token_file.exists()
    assert exchanges[0]["grant_type"] == "authorization_code"
    assert exchanges[0]["code"] == "test-code" and "code_verifier" in exchanges[0]
    assert opened and opened[0].startswith("https://accounts.google.com/")


async def test_login_rejects_a_mismatched_state(client_file, token_file):
    auth, http = auth_with(lambda request: token_response(refresh="r"), client_file, token_file)

    async def wrong_state(url: str) -> None:
        redirect = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))["redirect_uri"]
        parsed = urllib.parse.urlparse(redirect)
        reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
        writer.write(b"GET /?code=abc&state=other HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        await reader.read(100)
        writer.close()

    async with http:
        with pytest.raises(GoogleAuthError, match="맞지 않습니다"):
            await auth.login(lambda url: asyncio.get_running_loop().create_task(wrong_state(url)))


async def test_login_reports_a_cancelled_consent(client_file, token_file):
    auth, http = auth_with(lambda request: token_response(), client_file, token_file)

    async def denied(url: str) -> None:
        redirect = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))["redirect_uri"]
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))["state"]
        parsed = urllib.parse.urlparse(redirect)
        reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
        writer.write(f"GET /?error=access_denied&state={state} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        await writer.drain()
        await reader.read(100)
        writer.close()

    async with http:
        with pytest.raises(GoogleAuthError, match="취소"):
            await auth.login(lambda url: asyncio.get_running_loop().create_task(denied(url)))


async def test_login_without_refresh_token_explains_what_to_do(client_file, token_file):
    auth, http = auth_with(lambda request: token_response(), client_file, token_file)
    async with http:
        with pytest.raises(GoogleAuthError, match="refresh_token"):
            await auth.login(lambda url: asyncio.get_running_loop().create_task(visit(url)))


def test_configured_needs_both_files(client_file, token_file):
    auth = GoogleAuth(client_file, token_file, httpx.AsyncClient())
    assert auth.configured is False
    token_file.write_text(StoredToken("r").to_json(), encoding="utf-8")
    assert auth.configured is True
