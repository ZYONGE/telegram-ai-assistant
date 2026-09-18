"""메일 후속 동작 한 곳: 정리 내역 되돌리기, 답변 대기 관리, 답장 초안 저장.

브리핑·도구·확인 버튼이 모두 이 서비스를 쓴다. 메일 발송 기능은 없다.
"""

import logging
from collections.abc import Callable
from datetime import datetime, time, timedelta

from app.core.clock import KST, to_kst, utc_now
from app.google.accounts import GoogleAccounts
from app.google.auth import GoogleApiError, GoogleAuthError
from app.storage.mail import CleanupRecord, MailCleanupLog, MailStateStore, WaitingReply, WaitingReplyStore

logger = logging.getLogger(__name__)

# 되돌리기 버튼이 가리키는 날 (콜백 데이터에 들어간다)
UNDO_PREFIX = "undo:mail"


def day_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = datetime.combine(to_kst(now).date(), time(0), tzinfo=KST)
    return start, start + timedelta(days=1)


def undo_data(now: datetime) -> str:
    return f"{UNDO_PREFIX}:{to_kst(now):%Y%m%d}"


def parse_undo(data: str) -> datetime | None:
    """콜백 데이터에서 되돌릴 날짜를 읽는다."""
    prefix, _, day = data.rpartition(":")
    if prefix != UNDO_PREFIX or len(day) != 8 or not day.isdigit():
        return None
    try:
        return datetime.strptime(day, "%Y%m%d").replace(tzinfo=KST)
    except ValueError:
        return None


class MailService:
    def __init__(
        self,
        accounts: GoogleAccounts,
        cleanup: MailCleanupLog,
        waiting: WaitingReplyStore,
        state: MailStateStore,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._accounts = accounts
        self._cleanup = cleanup
        self._waiting = waiting
        self._state = state
        self._clock = clock

    async def cleaned_today(self, now: datetime | None = None) -> list[CleanupRecord]:
        start, _ = day_bounds(now or self._clock())
        return await self._cleanup.since(start)

    async def undo_cleanup(self, day_start: datetime) -> tuple[int, int]:
        """그날 휴지통으로 보낸 메일을 되돌린다. (되돌린 수, 실패한 수)"""
        now = self._clock()
        restored = failed = 0
        for record in await self._cleanup.since(day_start):
            account = self._accounts.find(record.account)
            if account is None or not account.connected:
                failed += 1
                continue
            try:
                await account.gmail.untrash(record.message_id)
            except (GoogleApiError, GoogleAuthError) as exc:
                logger.info("되돌리기 실패: %s", exc)
                failed += 1
                continue
            await self._cleanup.mark_undone(record.id, now)
            restored += 1
        return restored, failed

    async def waiting_items(self) -> list[WaitingReply]:
        return await self._waiting.open_items()

    async def resolve_waiting(self, waiting_id: int) -> WaitingReply | None:
        item = await self._waiting.get(waiting_id)
        if item is None or item.resolved_at is not None:
            return None
        await self._waiting.resolve(waiting_id, self._clock())
        return item

    async def overdue_waiting(self, now: datetime) -> list[WaitingReply]:
        """기한이 지났는데 아직 상기하지 않은 답변 대기."""
        return [
            item
            for item in await self._waiting.open_items()
            if item.due_at is not None and item.due_at <= now and item.reminded_at is None
        ]

    async def mark_reminded(self, items: list[WaitingReply], now: datetime) -> None:
        for item in items:
            await self._waiting.mark_reminded(item.id, now)

    async def save_draft(self, waiting_id: int, body: str) -> tuple[WaitingReply, str]:
        """답장 초안을 임시보관함에 저장한다. 발송은 하지 않는다."""
        item = await self._waiting.get(waiting_id)
        if item is None:
            raise ValueError(f"#{waiting_id} 답변 대기 항목이 없습니다.")
        account = self._accounts.find(item.account)
        if account is None or not account.connected:
            raise ValueError(f"{item.account} 계정이 연결되어 있지 않습니다.")
        draft_id = await account.gmail.create_draft(item.thread_id, item.sender, item.subject, body)
        return item, draft_id

    async def morning_list(self) -> list[tuple[str, str]]:
        return await self._state.unbriefed("other")

    async def mark_morning_listed(self) -> None:
        await self._state.mark_briefed("other")
