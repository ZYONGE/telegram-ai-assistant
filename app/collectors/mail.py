"""Gmail 수집기: 새 메일 → 규칙 판단 → 이벤트와 후속 동작.

- 모델을 부르지 않는다. 판단은 등록된 규칙(코드)으로만 한다 (CLAUDE.md 6절).
- 휴지통 이동은 여기(규칙 엔진)에서만 한다. 모델 도구로는 주지 않는다. 영구 삭제는 없다.
- 실패해도 예외를 밖으로 던지지 않고 collector_failed 이벤트로 보고한다.
- 메일 제목·본문은 외부 데이터다. 알림에 담아 보여 주기만 하고 지시로 해석하지 않는다 (절대 규칙 8).
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from app.core.clock import utc_now
from app.core.events import Event, EventKind, EventSource, collector_failed
from app.core.interfaces import MailAction, MailMessage
from app.google.accounts import GoogleAccounts
from app.google.auth import GoogleApiError, GoogleAuthError
from app.mail.rules import RuleEngine, Verdict
from app.storage.mail import MailCleanupLog, MailRuleRepository, MailStateStore, WaitingReplyStore

logger = logging.getLogger(__name__)

# 답변 대기 기본 기한
REPLY_DUE_DAYS = 3
# 한 번 돌 때 확인할 답변 대기 건수
MAX_REPLY_CHECKS = 10
SNIPPET_IN_ALERT = 160


def alert_text(message: MailMessage, verdict: Verdict, account: str, show_account: bool) -> tuple[str, str]:
    """알림 제목과 본문. 모델을 거치지 않고 코드가 만든다."""
    who = message.sender_name or message.sender
    where = f"[{account}] " if show_account else ""
    title = f"{where}{verdict.label}: {who}"
    lines = [message.subject or "(제목 없음)"]
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
    ) -> None:
        self._accounts = accounts
        self._rules = rules
        self._state = state
        self._cleanup = cleanup
        self._waiting = waiting
        self._protected = protected_domains
        self._clock = clock

    async def collect(self) -> list[Event]:
        if not self._accounts.ready:
            return []
        engine = RuleEngine(tuple(await self._rules.list_all()), self._protected)
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
            verdict = engine.classify(message, account.label)
            if not await self._state.mark_seen(account.label, message_id, verdict.kind, message.subject, now):
                continue
            events += await self._apply(account, message, verdict, now)
        await self._state.save_history_id(account.label, latest, now)
        events += await self._check_replies(account, now)
        return events

    async def _apply(self, account, message: MailMessage, verdict: Verdict, now: datetime) -> list[Event]:
        if MailAction.TRASH in verdict.actions:
            await account.gmail.trash(message.message_id)
            await self._cleanup.record(
                account.label, message.message_id, message.subject, message.sender, now
            )
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
