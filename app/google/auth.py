"""Google OAuth (설치형 앱, 루프백 흐름).

- 최초 1회만 브라우저로 로그인한다: `py -3.14 -m uv run python -m app.google.login`
- 클라이언트 파일(`private/google_client.json`)과 토큰(`private/google_token.json`)은 private/ 안에만 둔다.
- 실행 중에는 refresh_token으로 access_token만 갱신한다. 토큰 값은 로그·오류 메시지에 남기지 않는다.
- 외부 라이브러리 없이 표준 흐름(PKCE + 루프백 리디렉션)을 직접 구현한다 (docs/adr/0006).
"""

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import httpx

from app.core.clock import utc_now

logger = logging.getLogger(__name__)

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
# 읽기·라벨·휴지통·임시보관함까지. 영구 삭제와 발송 권한은 포함되지 않는다.
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
DEFAULT_SCOPES = (CALENDAR_SCOPE, GMAIL_SCOPE)

# 만료 직전에 미리 갱신한다
REFRESH_MARGIN = timedelta(minutes=2)
LOGIN_TIMEOUT = 300

LOGIN_NEEDED = (
    "Google 계정 연결이 아직 안 되어 있습니다. private/google_client.json을 두고 "
    "`py -3.14 -m uv run python -m app.google.login <계정 이름>`으로 계정마다 한 번씩 로그인해 주세요."
)
NO_CLIENT_FILE = (
    "Google 클라이언트 파일이 없습니다. Google Cloud 콘솔에서 만든 데스크톱 앱 OAuth 클라이언트 JSON을 "
    "private/google_client.json으로 저장해 주세요."
)
SUCCESS_PAGE = (
    "<html><head><meta charset='utf-8'><title>연결 완료</title></head>"
    "<body style='font-family:sans-serif'><h3>연결이 끝났습니다.</h3>"
    "<p>이 창을 닫고 텔레그램으로 돌아가세요.</p></body></html>"
)


class GoogleAuthError(Exception):
    """계정 연결 문제. 사용자가 해야 할 일을 메시지에 담는다."""


class GoogleApiError(Exception):
    """Google API 호출 실패. 응답 원문은 담지 않는다."""


class TransientGoogleError(GoogleApiError):
    """잠시 뒤 다시 하면 될 수 있는 실패 (서버 오류, 한도, 네트워크)."""


@dataclass(frozen=True, slots=True)
class ClientSecrets:
    client_id: str
    client_secret: str
    auth_uri: str = AUTH_URI
    token_uri: str = TOKEN_URI

    @classmethod
    def load(cls, path: Path) -> "ClientSecrets":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise GoogleAuthError(NO_CLIENT_FILE) from None
        except ValueError:
            raise GoogleAuthError(f"Google 클라이언트 파일을 읽지 못했습니다: {path}") from None
        block = raw.get("installed") or raw.get("web") or {}
        if not block.get("client_id") or not block.get("client_secret"):
            raise GoogleAuthError(f"Google 클라이언트 파일 형식이 다릅니다: {path} (데스크톱 앱 JSON이어야 합니다)")
        return cls(
            client_id=block["client_id"],
            client_secret=block["client_secret"],
            auth_uri=block.get("auth_uri", AUTH_URI),
            token_uri=block.get("token_uri", TOKEN_URI),
        )


@dataclass(frozen=True, slots=True)
class StoredToken:
    refresh_token: str
    access_token: str = ""
    expires_at: datetime | None = None
    scopes: tuple[str, ...] = field(default_factory=tuple)

    def is_valid(self, now: datetime) -> bool:
        return bool(self.access_token) and self.expires_at is not None and now + REFRESH_MARGIN < self.expires_at

    def to_json(self) -> str:
        return json.dumps(
            {
                "refresh_token": self.refresh_token,
                "access_token": self.access_token,
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
                "scopes": list(self.scopes),
            },
            ensure_ascii=False,
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "StoredToken":
        raw = json.loads(text)
        expires = raw.get("expires_at")
        return cls(
            refresh_token=raw["refresh_token"],
            access_token=raw.get("access_token", ""),
            expires_at=datetime.fromisoformat(expires) if expires else None,
            scopes=tuple(raw.get("scopes", ())),
        )


def pkce_pair() -> tuple[str, str]:
    """코드 검증자와 challenge(S256). 인증 코드가 가로채여도 쓰이지 못하게 한다."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def authorization_url(secrets_file: ClientSecrets, redirect_uri: str, challenge: str, state: str, scopes) -> str:
    query = {
        "client_id": secrets_file.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        # refresh_token을 받으려면 두 값이 필요하다
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{secrets_file.auth_uri}?{urllib.parse.urlencode(query)}"


def parse_redirect(request_line: str) -> dict[str, str]:
    """루프백으로 들어온 요청 첫 줄에서 질의 문자열을 꺼낸다."""
    parts = request_line.split(" ")
    target = parts[1] if len(parts) > 1 else ""
    query = urllib.parse.urlparse(target).query
    return {key: value[0] for key, value in urllib.parse.parse_qs(query).items()}


class GoogleAuth:
    """토큰을 보관하고 필요할 때 갱신한다. API 클라이언트는 access_token만 받아 쓴다."""

    def __init__(
        self,
        client_file: Path,
        token_file: Path,
        http: httpx.AsyncClient,
        scopes: tuple[str, ...] = DEFAULT_SCOPES,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._client_file = client_file
        self._token_file = token_file
        self._http = http
        self._scopes = scopes
        self._clock = clock
        self._token: StoredToken | None = None
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        """클라이언트 파일과 토큰이 모두 있으면 바로 쓸 수 있다."""
        return self._client_file.exists() and self._token_file.exists()

    def secrets(self) -> ClientSecrets:
        return ClientSecrets.load(self._client_file)

    def stored_token(self) -> StoredToken:
        try:
            return StoredToken.from_json(self._token_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise GoogleAuthError(LOGIN_NEEDED) from None
        except (ValueError, KeyError):
            raise GoogleAuthError(f"토큰 파일을 읽지 못했습니다: {self._token_file} (다시 로그인해 주세요)") from None

    def save_token(self, token: StoredToken) -> None:
        self._token_file.parent.mkdir(parents=True, exist_ok=True)
        self._token_file.write_text(token.to_json(), encoding="utf-8")
        self._token = token

    async def access_token(self) -> str:
        """유효한 access_token. 만료가 가까우면 갱신한다."""
        async with self._lock:
            token = self._token or self.stored_token()
            now = self._clock()
            if token.is_valid(now):
                self._token = token
                return token.access_token
            refreshed = await self._refresh(token)
            self.save_token(refreshed)
            return refreshed.access_token

    async def _refresh(self, token: StoredToken) -> StoredToken:
        secrets_file = self.secrets()
        data = {
            "client_id": secrets_file.client_id,
            "client_secret": secrets_file.client_secret,
            "refresh_token": token.refresh_token,
            "grant_type": "refresh_token",
        }
        payload = await self._token_request(secrets_file.token_uri, data, "토큰 갱신")
        logger.info("Google 토큰을 갱신했습니다")
        return StoredToken(
            refresh_token=payload.get("refresh_token", token.refresh_token),
            access_token=payload["access_token"],
            expires_at=self._clock() + timedelta(seconds=int(payload.get("expires_in", 3600))),
            scopes=tuple(payload.get("scope", " ".join(token.scopes)).split()),
        )

    async def _token_request(self, url: str, data: dict[str, str], what: str) -> dict[str, Any]:
        try:
            response = await self._http.post(url, data=data)
        except httpx.HTTPError as exc:
            raise TransientGoogleError(f"Google {what} 중 연결 실패 ({type(exc).__name__})") from None
        if response.status_code >= 500:
            raise TransientGoogleError(f"Google {what} 실패 (HTTP {response.status_code})")
        if response.status_code != 200:
            # invalid_grant = 사용자가 권한을 철회했거나 테스트 모드 토큰이 만료된 경우
            reason = ""
            try:
                reason = str(response.json().get("error", ""))
            except ValueError:
                pass
            if reason == "invalid_grant":
                raise GoogleAuthError("Google 연결이 만료되었습니다. 로그인을 다시 해 주세요 (python -m app.google.login).")
            raise GoogleAuthError(f"Google {what}에 실패했습니다 (HTTP {response.status_code}).")
        return response.json()

    async def login(self, open_browser: Callable[[str], Any], port: int = 0) -> StoredToken:
        """브라우저를 열어 1회 승인을 받고 refresh_token을 저장한다."""
        secrets_file = self.secrets()
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(16)
        received: asyncio.Future = asyncio.get_running_loop().create_future()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                request_line = (await reader.readline()).decode("utf-8", "replace")
                params = parse_redirect(request_line)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n")
                writer.write(SUCCESS_PAGE.encode("utf-8"))
                await writer.drain()
                if not received.done():
                    if params.get("state") != state:
                        received.set_exception(GoogleAuthError("로그인 응답이 요청과 맞지 않습니다. 다시 시도해 주세요."))
                    elif "code" in params:
                        received.set_result(params["code"])
                    else:
                        received.set_exception(
                            GoogleAuthError(f"승인이 취소되었습니다 ({params.get('error', '이유 없음')}).")
                        )
            finally:
                writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", port)
        async with server:
            bound = server.sockets[0].getsockname()[1]
            redirect_uri = f"http://127.0.0.1:{bound}/"
            open_browser(authorization_url(secrets_file, redirect_uri, challenge, state, self._scopes))
            try:
                code = await asyncio.wait_for(received, timeout=LOGIN_TIMEOUT)
            except TimeoutError:
                raise GoogleAuthError("로그인을 기다리다 시간이 지났습니다. 다시 실행해 주세요.") from None

        payload = await self._token_request(
            secrets_file.token_uri,
            {
                "client_id": secrets_file.client_id,
                "client_secret": secrets_file.client_secret,
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            "로그인",
        )
        if "refresh_token" not in payload:
            raise GoogleAuthError("refresh_token을 받지 못했습니다. 계정 권한을 지우고 다시 로그인해 주세요.")
        token = StoredToken(
            refresh_token=payload["refresh_token"],
            access_token=payload.get("access_token", ""),
            expires_at=self._clock() + timedelta(seconds=int(payload.get("expires_in", 3600))),
            scopes=tuple(payload.get("scope", " ".join(self._scopes)).split()),
        )
        self.save_token(token)
        return token
