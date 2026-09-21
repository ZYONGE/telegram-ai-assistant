"""Gmail 수집기: 새 메일 → 규칙 판단 → 이벤트와 후속 동작.

- 모델을 부르지 않는다. 판단은 등록된 규칙(코드)으로만 한다 (CLAUDE.md 6절).
- 휴지통 이동은 여기(규칙 엔진)에서만 한다. 모델 도구로는 주지 않는다. 영구 삭제는 없다.
- 실패해도 예외를 밖으로 던지지 않고 collector_failed 이벤트로 보고한다.
- 메일 제목·본문은 외부 데이터다. 알림에 담아 보여 주기만 하고 지시로 해석하지 않는다 (절대 규칙 8).
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.core.clock import utc_now
from app.core.events import Event, EventKind, EventSource, collector_failed
from app.core.interfaces import MailAction, MailMessage
from app.google.accounts import GoogleAccounts
from app.google.auth import GoogleApiError, GoogleAuthError
from app.mail.rules import OTHER, AutoPolicy, RuleEngine, Verdict
from app.storage.mail import MailCleanupLog, MailRuleRepository, MailStateStore, WaitingReplyStore

logger = logging.getLogger(__name__)

# 답변 대기 기본 기한
REPLY_DUE_DAYS = 3
# 한 번 돌 때 확인할 답변 대기 건수
MAX_REPLY_CHECKS = 10
SNIPPET_IN_ALERT = 300
# 스레드 전체를 볼 때의 기준 시각 (처음부터)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def alert_text(message: MailMessage, verdict: Verdict, account: str, show_account: bool) -> tuple[str, str]:
    """알림 제목과 본문. 모델을 거치지 않고 코드가 만든다."""
    who = f"{message.sender_name} <{message.sender}>" if message.sender_name else message.sender
    where = f"[{account}] " if show_account else ""
    title = f"{where}{verdict.label}: {who}"
    lines = [f"제목: {message.subject or '(제목 없음)'}"]
    if message.snippet:
        lines.append(message.snippet[:SNIPPET_IN_ALERT])
    if MailAction.TRACK_REPLY in verdict.actions:
        lines.append("답변 대기로 등록했습니다.")
    if MailAction.SUGGEST_SCHEDULE in verdict.actions:
        lines.append("일정이나 할 일로 등록할까요?")
    if verdict.trash_blocked:
        lines.append("보호 목록이라 휴지통으로 보내지 않았습니다.")
    elif MailAction.TRASH in verdict.actions:
        lines.append("규칙에 따라 휴지통으로 옮겼습니다.")
    if MailAction.MARK_IMPORTANT in verdict.actions:
        lines.append("중요 표시를 해 두었습니다.")
    return title, "\n".join(lines)


class MailCollector:
    name = "gmail"

    def __init__(
        self,
        accounts: GoogleAccounts,
        rules: MailRuleRepository,
        state: MailStateStore,
        cleanup: MailCleanupLog,
        waiting: WaitingReplyStore,
        protected_domains: frozenset[str] = frozenset(),
        clock: Callable[[], datetime] = utc_now,
        auto: AutoPolicy | None = None,
        receipt_label: str = "Receipt",
    ) -> None:
        self._accounts = accounts
        self._rules = rules
        self._state = state
        self._cleanup = cleanup
        self._waiting = waiting
        self._protected = protected_domains
        self._clock = clock
        self._auto = auto or AutoPolicy()
        self._receipt_label = receipt_label

    async def collect(self) -> list[Event]:
        if not self._accounts.ready:
            return []
        engine = RuleEngine(tuple(await self._rules.list_all()), self._protected, self._auto)
        events: list[Event] = []
        for account in self._accounts.connected:
            try:
                events += await self._collect_account(account, engine)
            except GoogleAuthError as exc:
                events.append(collector_failed(self.name, "auth", str(exc)))
            except GoogleApiError as exc:
                events.append(collector_failed(self.name, "api", str(exc)))
            except Exception:
                logger.exception("메일 수집 실패: %s", account.label)
                events.append(collector_failed(self.name, "unknown", f"{account.label} 계정 메일 확인 실패"))
        return events

    async def _collect_account(self, account, engine: RuleEngine) -> list[Event]:
        now = self._clock()
        gmail = account.gmail
        cursor = await self._state.history_id(account.label)
        if cursor is None:
            # 처음 연결한 계정은 지금 시점부터 본다. 지난 메일을 한꺼번에 알리지 않는다.
            await self._state.save_history_id(account.label, await gmail.current_history_id(), now)
            logger.info("%s 계정 메일 수집을 시작합니다", account.label)
            return []

        message_ids, latest = await gmail.new_message_ids(cursor)
        events: list[Event] = []
        for message_id in message_ids:
            message = await gmail.message(message_id)
            verdict = await self._spare_conversations(gmail, message, engine.classify(message, account.label))
            if not await self._state.mark_seen(account.label, message_id, verdict.kind, message.subject, now):
                continue
            events += await self._apply(account, message, verdict, now)
        await self._state.save_history_id(account.label, latest, now)
        events += await self._check_replies(account, now)
        return events

    async def _spare_conversations(self, gmail, message: MailMessage, verdict: Verdict) -> Verdict:
        """스팸함으로 보낼 메일이라도, 사용자가 답장한 적 있는 대화면 받은편지함에 둔다."""
        if MailAction.SPAM not in verdict.actions:
            return verdict
        try:
            if not await gmail.thread_has_reply(message.thread_id, EPOCH):
                return verdict
        except GoogleApiError:
            pass  # 확인하지 못했으면 옮기지 않는 쪽을 고른다
        return Verdict(OTHER, (MailAction.MORNING_LIST,), f"{verdict.reason} (주고받은 대화라 옮기지 않음)")

    async def _apply(self, account, message: MailMessage, verdict: Verdict, now: datetime) -> list[Event]:
        gmail = account.gmail
        moved = ""
        label = ""
        if MailAction.TRASH in verdict.actions:
            await gmail.trash(message.message_id)
            moved = "trash"
        elif MailAction.SPAM in verdict.actions:
            await gmail.spam(message.message_id)
            moved = "spam"
        elif MailAction.FILE_RECEIPT in verdict.actions:
            label = await gmail.label_id(self._receipt_label)
            await gmail.file_under(message.message_id, label)
            moved = "file"
        if moved:
            await self._cleanup.record(
                account.label, message.message_id, message.subject, message.sender, now, moved, label
            )
        if MailAction.MARK_IMPORTANT in verdict.actions:
            await gmail.mark_important(message.message_id)
        if MailAction.TRACK_REPLY in verdict.actions:
            await self._waiting.add(
                account.label,
                message.thread_id,
                message.message_id,
                message.subject,
                message.sender,
                now,
                due_at=now + timedelta(days=REPLY_DUE_DAYS),
            )
        if MailAction.NOTIFY not in verdict.actions:
            # 아침 목록·저녁 정리 내역은 브리핑이 저장소에서 읽어 간다
            return []
        title, body = alert_text(message, verdict, account.label, self._accounts.multiple)
        return [
            Event(
                source=EventSource.GMAIL,
                kind=EventKind.MAIL,
                title=title,
                body=body,
                urgent=verdict.urgent,
                ref_id=f"gmail:{account.label}:{message.message_id}",
                meta={"kind": verdict.kind, "reason": verdict.reason, "thread_id": message.thread_id},
            )
        ]

    async def _check_replies(self, account, now: datetime) -> list[Event]:
        """답변 대기 스레드에 답장이 생겼으면 자동으로 해제한다."""
        items = [item for item in await self._waiting.open_items() if item.account == account.label]
        for item in items[:MAX_REPLY_CHECKS]:
            try:
                if await account.gmail.thread_has_reply(item.thread_id, item.created_at):
                    await self._waiting.resolve(item.id, now)
                    # 제목은 남기지 않는다. 로그는 개인 파일이지만 굳이 옮겨 적을 까닭이 없다.
                    logger.info("답변 대기 1건을 해제했습니다 (%s 계정)", account.label)
            except GoogleApiError:
                continue
        return []
