"""Gmail 호출.

- 읽기·검색, 라벨 붙이고 떼기(보관처리·스팸함·읽음·별표·중요 표시), 휴지통 이동과 되돌리기, 답장 초안 저장만 한다.
  **메일 발송과 영구 삭제는 만들지 않는다** (CLAUDE.md 절대 규칙 5).
- 메일 본문과 제목은 외부에서 온 데이터다. 여기서는 그대로 담아 넘기고, 판단은 규칙 엔진이 한다 (절대 규칙 8).
- 계정 주소는 저장하지 않는다. 계정 구분은 사용자가 붙인 이름으로만 한다.
"""

import base64
import logging
import re
from datetime import UTC, datetime
from email.message import EmailMessage

import httpx
from bs4 import BeautifulSoup

from app.core.interfaces import MailMessage
from app.google.auth import GoogleApiError, GoogleAuth, GoogleAuthError, TransientGoogleError

logger = logging.getLogger(__name__)

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
# 한 번에 처리할 최대 메일 수. 밀린 메일이 많아도 한 번에 쏟아내지 않는다.
MAX_PER_RUN = 25
SNIPPET_LIMIT = 300
# 보낸 편지함·임시보관함·휴지통에 있는 메일은 새 메일로 보지 않는다
SKIP_LABELS = {"SENT", "DRAFT", "TRASH"}


class GmailClient:
    def __init__(self, auth: GoogleAuth, http: httpx.AsyncClient) -> None:
        self._auth = auth
        self._http = http
        # 라벨 이름 → ID. 한 번 찾으면 다시 묻지 않는다.
        self._labels: dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return self._auth.configured

    async def current_history_id(self) -> str:
        payload = await self._request("GET", "/profile")
        return str(payload.get("historyId", ""))

    async def new_message_ids(self, history_id: str) -> tuple[list[str], str]:
        """마지막으로 본 지점 이후에 온 메일 ID. 커서가 너무 오래됐으면 최근 메일로 대신 받는다."""
        try:
            payload = await self._request(
                "GET",
                "/history",
                params={"startHistoryId": history_id, "historyTypes": "messageAdded", "maxResults": "100"},
            )
        except GoogleApiError as exc:
            if "404" not in str(exc):
                raise
            logger.info("Gmail 커서가 만료되어 최근 메일부터 다시 봅니다")
            return await self.recent_message_ids(), await self.current_history_id()

        ids: list[str] = []
        for record in payload.get("history", []):
            for added in record.get("messagesAdded", []):
                message = added.get("message", {})
                if message.get("id") and not SKIP_LABELS & set(message.get("labelIds", [])):
                    ids.append(message["id"])
        latest = str(payload.get("historyId", history_id))
        # 오래된 것부터 처리한다
        return list(dict.fromkeys(reversed(ids)))[:MAX_PER_RUN], latest

    async def recent_message_ids(
        self, query: str = "newer_than:1d -in:sent -in:draft", limit: int = MAX_PER_RUN
    ) -> list[str]:
        """Gmail 검색어로 메일을 찾는다 (from:, subject:, is:unread, label:, newer_than: 등 Gmail 문법 그대로)."""
        payload = await self._request(
            "GET", "/messages", params={"q": query, "maxResults": str(min(limit, MAX_PER_RUN))}
        )
        return [item["id"] for item in payload.get("messages", []) if item.get("id")]

    async def content(self, message_id: str) -> tuple[str, list[str]]:
        """본문 글과 첨부 파일 이름. 첨부는 내려받지 않는다. 외부에서 온 글이다 (절대 규칙 8)."""
        payload = await self._request("GET", f"/messages/{message_id}", params={"format": "full"})
        return read_content(payload.get("payload", {}))

    async def labels(self) -> dict[str, str]:
        """라벨 ID → 이름. 사용자가 만든 라벨과 시스템 라벨을 함께 준다."""
        payload = await self._request("GET", "/labels")
        found = {str(label["id"]): str(label.get("name", "")) for label in payload.get("labels", []) if label.get("id")}
        self._labels.update({name: label_id for label_id, name in found.items()})
        return found

    async def find_label(self, name: str) -> str:
        """이름으로 라벨을 찾는다. 없으면 빈 문자열 (만들지 않는다)."""
        if name not in self._labels:
            await self.labels()
        return self._labels.get(name, "")

    async def message(self, message_id: str) -> MailMessage:
        payload = await self._request(
            "GET",
            f"/messages/{message_id}",
            params={
                "format": "metadata",
                "metadataHeaders": ["From", "Subject", "Date", "To"],
            },
        )
        return parse_message(payload)

    async def thread_has_reply(self, thread_id: str, after: datetime) -> bool:
        """스레드에 사용자가 보낸 메일이 생겼는지. 답변 대기 자동 해제에 쓴다."""
        payload = await self._request("GET", f"/threads/{thread_id}", params={"format": "minimal"})
        for message in payload.get("messages", []):
            labels = set(message.get("labelIds", []))
            when = _internal_date(message.get("internalDate"))
            if "SENT" in labels and when is not None and when >= after:
                return True
        return False

    async def trash(self, message_id: str) -> None:
        await self._request("POST", f"/messages/{message_id}/trash")

    async def untrash(self, message_id: str) -> None:
        await self._request("POST", f"/messages/{message_id}/untrash")

    async def modify(self, message_id: str, add: tuple[str, ...] = (), remove: tuple[str, ...] = ()) -> None:
        """라벨을 붙이고 뗀다. 스팸함·보관함 이동과 중요 표시가 모두 이것이다. 지우지는 않는다."""
        await self._request(
            "POST", f"/messages/{message_id}/modify", json={"addLabelIds": list(add), "removeLabelIds": list(remove)}
        )

    async def spam(self, message_id: str) -> None:
        await self.modify(message_id, add=("SPAM",), remove=("INBOX",))

    async def unspam(self, message_id: str) -> None:
        await self.modify(message_id, add=("INBOX",), remove=("SPAM",))

    async def mark_important(self, message_id: str) -> None:
        await self.modify(message_id, add=("IMPORTANT",))

    async def file_under(self, message_id: str, label_id: str) -> None:
        """보관함(라벨)으로 옮긴다. 받은편지함에서만 빠지고 메일은 그대로 있다."""
        await self.modify(message_id, add=(label_id,), remove=("INBOX",))

    async def unfile(self, message_id: str, label_id: str) -> None:
        await self.modify(message_id, add=("INBOX",), remove=(label_id,))

    async def label_id(self, name: str) -> str:
        """이름으로 라벨을 찾고, 없으면 만든다."""
        if name in self._labels:
            return self._labels[name]
        payload = await self._request("GET", "/labels")
        for label in payload.get("labels", []):
            if label.get("name") == name and label.get("id"):
                self._labels[name] = str(label["id"])
                return self._labels[name]
        created = await self._request(
            "POST",
            "/labels",
            json={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
        )
        self._labels[name] = str(created.get("id", ""))
        logger.info("Gmail 보관함을 만들었습니다")
        return self._labels[name]

    async def create_draft(self, thread_id: str, to: str, subject: str, body: str) -> str:
        """답장 초안을 임시보관함에 저장한다. 발송은 하지 않는다."""
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject if subject.startswith("Re:") else f"Re: {subject}"
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        payload = await self._request(
            "POST", "/drafts", json={"message": {"raw": raw, "threadId": thread_id}}
        )
        return str(payload.get("id", ""))

    async def create_new_draft(self, to: str, subject: str, body: str) -> str:
        """새 메일 초안을 임시보관함에 저장한다. 발송은 하지 않는다 (CLAUDE.md 절대 규칙 5)."""
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        payload = await self._request("POST", "/drafts", json={"message": {"raw": raw}})
        return str(payload.get("id", ""))

    async def _request(self, method: str, path: str, params: dict | None = None, json: dict | None = None) -> dict:
        token = await self._auth.access_token()
        try:
            response = await self._http.request(
                method, GMAIL_API + path, params=params, json=json, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            raise TransientGoogleError(f"Gmail 연결 실패 ({type(exc).__name__})") from None
        if response.status_code in (200, 201):
            try:
                return response.json()
            except ValueError:
                raise GoogleApiError("Gmail 응답을 읽지 못했습니다.") from None
        if response.status_code == 204:
            return {}
        if response.status_code == 401:
            raise GoogleAuthError("Google 연결이 만료되었습니다. python -m app.google.login으로 다시 연결해 주세요.")
        if response.status_code in (403, 429) or response.status_code >= 500:
            raise TransientGoogleError(f"Gmail 요청이 잠시 막혔습니다 (HTTP {response.status_code}).")
        raise GoogleApiError(f"Gmail 요청 오류 (HTTP {response.status_code}).")


def parse_message(payload: dict) -> MailMessage:
    headers = {
        item.get("name", "").lower(): item.get("value", "")
        for item in payload.get("payload", {}).get("headers", [])
    }
    name, address = split_sender(headers.get("from", ""))
    received = _internal_date(payload.get("internalDate")) or datetime.now(UTC)
    return MailMessage(
        message_id=str(payload.get("id", "")),
        thread_id=str(payload.get("threadId", "")),
        sender=address,
        sender_name=name,
        subject=headers.get("subject", "").strip(),
        snippet=(payload.get("snippet") or "").strip()[:SNIPPET_LIMIT],
        received_at=received,
        labels=frozenset(payload.get("labelIds", [])),
    )


def split_sender(value: str) -> tuple[str, str]:
    """'홍길동 <hong@example.com>' → ('홍길동', 'hong@example.com')"""
    text = value.strip()
    if "<" in text and ">" in text:
        name = text[: text.index("<")].strip().strip('"')
        address = text[text.index("<") + 1 : text.rindex(">")].strip()
        return name, address.lower()
    return "", text.lower()


def _internal_date(value: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError):
        return None


# 모델에 넘길 본문 길이. 긴 메일은 앞부분이면 요지를 잡기에 충분하다.
BODY_LIMIT = 4000
_CHARSET = re.compile(r"charset=\"?([\w-]+)", re.IGNORECASE)


def read_content(part: dict) -> tuple[str, list[str]]:
    """메일 본문(평문 우선, 없으면 HTML에서 글자만)과 첨부 파일 이름을 모은다."""
    plain: list[str] = []
    html: list[str] = []
    attachments: list[str] = []

    def walk(node: dict) -> None:
        mime = str(node.get("mimeType", "")).lower()
        filename = str(node.get("filename") or "")
        if filename:
            attachments.append(filename)
        elif mime == "text/plain":
            plain.append(_decode(node))
        elif mime == "text/html":
            html.append(_decode(node))
        for child in node.get("parts", []) or []:
            walk(child)

    walk(part)
    text = "\n".join(plain).strip()
    if not text and html:
        text = BeautifulSoup("\n".join(html), "html.parser").get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > BODY_LIMIT:
        text = text[:BODY_LIMIT] + "…"
    return text, attachments


def _decode(node: dict) -> str:
    data = str(node.get("body", {}).get("data", ""))
    if not data:
        return ""
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    headers = {item.get("name", "").lower(): item.get("value", "") for item in node.get("headers", [])}
    found = _CHARSET.search(headers.get("content-type", ""))
    try:
        return raw.decode(found.group(1) if found else "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")
