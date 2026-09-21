"""메일함 조작: 사용자가 대화로 시키는 대로 메일을 찾고, 읽고, 옮기고, 표시한다 (사용자 결정 2026-09-22).

- 규칙 엔진(`app/mail/rules.py`)은 새 메일을 저절로 나누고, 이 모듈은 사용자가 시킨 것을 한다. 둘은 따로 돈다.
- **발송과 영구 삭제는 없다** (CLAUDE.md 절대 규칙 5). 지우는 것은 휴지통까지이고, 휴지통은 30일 안에 되살릴 수 있다.
- 휴지통·스팸함 이동은 도구에서 확인 버튼을 거친다. 메일 본문에 "전부 지워라" 같은 글이 있어도 모델이 혼자 지우지 못한다.
- 메일은 `계정:메일ID`로 가리킨다. 비서를 다시 켜도, 확인 버튼을 나중에 눌러도 같은 메일을 가리킨다.
- 메일 제목·본문은 외부에서 온 데이터다. 보여 주기만 하고 지시로 다루지 않는다 (절대 규칙 8).
"""

from dataclasses import dataclass

from app.core.clock import format_kst
from app.core.interfaces import MailMessage
from app.google.accounts import GoogleAccounts
from app.google.auth import GoogleApiError, GoogleAuthError

# 한 번에 다룰 수 있는 메일 수. 넘으면 나눠서 시킨다.
MAX_TARGETS = 50
MAX_RESULTS = 25

# 라벨만 붙이고 떼는 동작: (붙일 것, 뗄 것)
LABEL_ACTIONS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "archive": ((), ("INBOX",)),
    "inbox": (("INBOX",), ()),
    "read": ((), ("UNREAD",)),
    "unread": (("UNREAD",), ()),
    "star": (("STARRED",), ()),
    "unstar": ((), ("STARRED",)),
    "important": (("IMPORTANT",), ()),
    "unimportant": ((), ("IMPORTANT",)),
    "not_spam": (("INBOX",), ("SPAM",)),
}
# 사용자가 이름을 준 라벨을 쓰는 동작
NAMED_ACTIONS = ("label", "unlabel", "move")
# 휴지통에서 꺼내기. 휴지통 보내기와 스팸함 보내기는 확인 버튼이 있는 도구가 따로 맡는다.
RESTORE_ACTIONS = ("untrash",)
ACTIONS = (*LABEL_ACTIONS, *NAMED_ACTIONS, *RESTORE_ACTIONS)
ACTION_NAMES = {
    "archive": "보관처리(받은편지함에서 빼기)",
    "inbox": "받은편지함으로",
    "read": "읽음 표시",
    "unread": "안읽음 표시",
    "star": "별표",
    "unstar": "별표 해제",
    "important": "중요 표시",
    "unimportant": "중요 표시 해제",
    "not_spam": "스팸 아님(받은편지함으로)",
    "label": "라벨 붙이기",
    "unlabel": "라벨 떼기",
    "move": "라벨로 옮기기(받은편지함에서 빼기)",
    "untrash": "휴지통에서 꺼내기",
    "trash": "휴지통으로",
    "spam": "스팸함으로",
}
# 목록에 상태로 보여 줄 시스템 라벨
FLAGS = {
    "UNREAD": "안읽음",
    "STARRED": "별표",
    "IMPORTANT": "중요",
    "SPAM": "스팸함",
    "TRASH": "휴지통",
    "SENT": "보낸메일",
    "DRAFT": "임시보관",
}
SYSTEM_PREFIXES = ("CATEGORY_", "CHAT")


class MailboxError(ValueError):
    """찾는 계정·메일·라벨이 없을 때. 비서가 사용자에게 그대로 옮길 수 있게 쓴다."""


@dataclass(frozen=True, slots=True)
class MailRef:
    account: str
    message_id: str

    def __str__(self) -> str:
        return f"{self.account}:{self.message_id}"


def parse_ref(text: str) -> MailRef:
    account, sep, message_id = str(text).strip().rpartition(":")
    if not sep or not account or not message_id.isalnum():
        raise MailboxError(f"메일 번호 '{text}'를 읽지 못했습니다. search_mail이 준 '계정:ID'를 그대로 넘겨 주세요.")
    return MailRef(account, message_id)


@dataclass(frozen=True, slots=True)
class Found:
    ref: MailRef
    message: MailMessage
    # 사용자가 만든 라벨 이름
    labels: tuple[str, ...] = ()

    def render(self) -> str:
        who = f"{self.message.sender_name} <{self.message.sender}>" if self.message.sender_name else self.message.sender
        state = [name for label, name in FLAGS.items() if label in self.message.labels]
        if "INBOX" not in self.message.labels and not {"SPAM", "TRASH", "SENT", "DRAFT"} & self.message.labels:
            state.append("보관됨")
        state += [f"라벨 {name}" for name in self.labels]
        parts = [str(self.ref), who, self.message.subject or "(제목 없음)", format_kst(self.message.received_at)]
        if state:
            parts.append(", ".join(state))
        return " · ".join(parts)


@dataclass(slots=True)
class Outcome:
    done: list[MailRef]
    failed: list[tuple[MailRef, str]]

    def render(self, action: str) -> str:
        text = f"{ACTION_NAMES.get(action, action)}: {len(self.done)}건 처리했습니다."
        if self.failed:
            reasons = "; ".join(f"{ref} ({reason})" for ref, reason in self.failed[:5])
            text += f" {len(self.failed)}건은 못 했습니다: {reasons}"
        return text


class Mailbox:
    def __init__(self, accounts: GoogleAccounts) -> None:
        self._accounts = accounts

    def _account(self, label: str):
        account = self._accounts.find(label)
        if account is None or not account.connected:
            names = ", ".join(self._accounts.labels)
            raise MailboxError(f"'{label}' 계정이 연결되어 있지 않습니다. 계정: {names}")
        return account

    async def search(self, query: str, account: str | None = None, limit: int = 10) -> list[Found]:
        """Gmail 검색어 그대로 찾는다. 계정을 주지 않으면 연결된 계정을 모두 본다."""
        targets = [self._account(account)] if account else self._accounts.connected
        found: list[Found] = []
        for target in targets:
            names = await self._user_labels(target)
            for message_id in await target.gmail.recent_message_ids(query, min(limit, MAX_RESULTS)):
                message = await target.gmail.message(message_id)
                mine = tuple(names[label] for label in message.labels if label in names)
                found.append(Found(MailRef(target.label, message_id), message, mine))
        found.sort(key=lambda item: item.message.received_at, reverse=True)
        return found[: max(limit, 1) * max(len(targets), 1)]

    async def read(self, ref: MailRef) -> tuple[Found, str, list[str]]:
        account = self._account(ref.account)
        message = await account.gmail.message(ref.message_id)
        names = await self._user_labels(account)
        body, attachments = await account.gmail.content(ref.message_id)
        mine = tuple(names[label] for label in message.labels if label in names)
        return Found(ref, message, mine), body, attachments

    async def labels(self, account: str) -> list[str]:
        return sorted((await self._user_labels(self._account(account))).values())

    async def apply(self, refs: list[MailRef], action: str, label: str = "") -> Outcome:
        """여러 메일에 같은 동작. 하나가 실패해도 나머지는 계속한다."""
        if action not in (*ACTIONS, "trash", "spam"):
            raise MailboxError(f"'{action}'은 할 수 없는 동작입니다. 가능한 것: {', '.join(ACTIONS)}")
        if not refs:
            raise MailboxError("어느 메일인지 알려 주세요.")
        if len(refs) > MAX_TARGETS:
            raise MailboxError(f"한 번에 {MAX_TARGETS}건까지만 다룹니다. 나눠서 시켜 주세요.")
        if action in NAMED_ACTIONS and not label.strip():
            raise MailboxError("어느 라벨인지 이름을 알려 주세요.")

        outcome = Outcome([], [])
        for ref in refs:
            try:
                account = self._account(ref.account)
                await self._apply_one(account.gmail, ref.message_id, action, label.strip())
            except MailboxError as exc:
                outcome.failed.append((ref, str(exc)))
            except (GoogleApiError, GoogleAuthError) as exc:
                outcome.failed.append((ref, str(exc)))
            else:
                outcome.done.append(ref)
        return outcome

    async def _apply_one(self, gmail, message_id: str, action: str, label: str) -> None:
        if action in LABEL_ACTIONS:
            add, remove = LABEL_ACTIONS[action]
            await gmail.modify(message_id, add=add, remove=remove)
        elif action == "label":
            await gmail.modify(message_id, add=(await gmail.label_id(label),))
        elif action == "move":
            await gmail.modify(message_id, add=(await gmail.label_id(label),), remove=("INBOX",))
        elif action == "unlabel":
            label_id = await gmail.find_label(label)
            if not label_id:
                raise MailboxError(f"'{label}' 라벨이 없습니다.")
            await gmail.modify(message_id, remove=(label_id,))
        elif action == "untrash":
            await gmail.untrash(message_id)
        elif action == "trash":
            await gmail.trash(message_id)
        elif action == "spam":
            await gmail.spam(message_id)

    async def draft_reply(self, ref: MailRef, body: str) -> tuple[MailMessage, str]:
        """그 메일에 대한 답장 초안을 임시보관함에 저장한다. 발송은 하지 않는다."""
        account = self._account(ref.account)
        message = await account.gmail.message(ref.message_id)
        draft_id = await account.gmail.create_draft(message.thread_id, message.sender, message.subject, body)
        return message, draft_id

    async def draft_new(self, account: str, to: str, subject: str, body: str) -> str:
        """새 메일 초안을 그 계정의 임시보관함에 저장한다. 발송은 하지 않는다."""
        return await self._account(account).gmail.create_new_draft(to, subject, body)

    async def _user_labels(self, account) -> dict[str, str]:
        """사용자가 만든 라벨만 (ID → 이름). 시스템 라벨은 상태로 따로 보여 준다."""
        found = await account.gmail.labels()
        return {
            label_id: name
            for label_id, name in found.items()
            if label_id.startswith("Label_") and not name.startswith(SYSTEM_PREFIXES)
        }

