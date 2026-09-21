"""메일 테스트용 가짜 계정과 Gmail 클라이언트."""

from dataclasses import dataclass, field
from datetime import datetime

from app.core.interfaces import MailMessage
from app.google.auth import GoogleApiError
from tests.conftest import kst


def message(
    message_id: str = "msg-1",
    sender: str = "prof@example.ac.kr",
    subject: str = "면담 일정",
    snippet: str = "다음 주 면담 안내드립니다.",
    sender_name: str = "홍길동",
    thread_id: str = "thread-1",
    received_at: datetime | None = None,
    labels: frozenset[str] = frozenset(),
) -> MailMessage:
    return MailMessage(
        message_id=message_id,
        thread_id=thread_id,
        sender=sender,
        sender_name=sender_name,
        subject=subject,
        snippet=snippet,
        received_at=received_at or kst(9, 18, 9),
        labels=labels,
    )


class FakeGmail:
    def __init__(self, messages: list[MailMessage] | None = None, history_id: str = "100") -> None:
        self.messages = {item.message_id: item for item in (messages or [])}
        self.history_id = history_id
        self.trashed: list[str] = []
        self.untrashed: list[str] = []
        self.spammed: list[str] = []
        self.unspammed: list[str] = []
        self.filed: list[tuple[str, str]] = []
        self.unfiled: list[tuple[str, str]] = []
        self.important: list[str] = []
        self.labels: dict[str, str] = {}
        self.drafts: list[tuple[str, str, str, str]] = []
        self.replied_threads: set[str] = set()
        self.fail_with: Exception | None = None
        self.configured = True

    async def current_history_id(self) -> str:
        return self.history_id

    async def new_message_ids(self, cursor: str) -> tuple[list[str], str]:
        if self.fail_with:
            raise self.fail_with
        return list(self.messages), self.history_id

    async def recent_message_ids(self, query: str = "") -> list[str]:
        if self.fail_with:
            raise self.fail_with
        return list(self.messages)

    async def message(self, message_id: str) -> MailMessage:
        return self.messages[message_id]

    async def trash(self, message_id: str) -> None:
        self.trashed.append(message_id)

    async def untrash(self, message_id: str) -> None:
        if self.fail_with:
            raise self.fail_with
        self.untrashed.append(message_id)

    async def spam(self, message_id: str) -> None:
        self.spammed.append(message_id)

    async def unspam(self, message_id: str) -> None:
        self.unspammed.append(message_id)

    async def mark_important(self, message_id: str) -> None:
        self.important.append(message_id)

    async def label_id(self, name: str) -> str:
        return self.labels.setdefault(name, f"Label_{len(self.labels) + 1}")

    async def file_under(self, message_id: str, label_id: str) -> None:
        self.filed.append((message_id, label_id))

    async def unfile(self, message_id: str, label_id: str) -> None:
        self.unfiled.append((message_id, label_id))

    async def thread_has_reply(self, thread_id: str, after: datetime) -> bool:
        return thread_id in self.replied_threads

    async def create_draft(self, thread_id: str, to: str, subject: str, body: str) -> str:
        self.drafts.append((thread_id, to, subject, body))
        return f"draft-{len(self.drafts)}"


@dataclass
class FakeAccount:
    label: str
    gmail: FakeGmail = field(default_factory=FakeGmail)
    connected: bool = True
    default: bool = False


class FakeAccounts:
    """GoogleAccounts에서 메일 기능이 쓰는 부분만 흉내 낸다."""

    def __init__(self, *accounts: FakeAccount) -> None:
        self.accounts = list(accounts)

    @property
    def connected(self) -> list[FakeAccount]:
        return [account for account in self.accounts if account.connected]

    @property
    def ready(self) -> bool:
        return bool(self.connected)

    @property
    def multiple(self) -> bool:
        return len(self.connected) > 1

    @property
    def labels(self) -> list[str]:
        return [account.label for account in self.accounts]

    def find(self, label: str | None):
        if not label:
            return None
        return next((account for account in self.accounts if account.label == label), None)

    def default_account(self):
        return next((account for account in self.connected if account.default), None) or (
            self.connected[0] if self.connected else None
        )


def api_error(message_text: str = "잠시 오류") -> GoogleApiError:
    return GoogleApiError(message_text)
