"""메일 관련 저장소: 규칙, 수집 커서, 처리 기록, 답변 대기.

메일 본문은 저장하지 않는다. 제목과 발신자처럼 알림에 필요한 만큼만 남긴다.
"""

import json
from dataclasses import dataclass
from datetime import datetime

import aiosqlite

from app.mail.rules import MailRule
from app.storage.db import Database, from_db_time, to_db_time


@dataclass(frozen=True, slots=True)
class CleanupRecord:
    id: int
    account: str
    message_id: str
    subject: str
    sender: str
    done_at: datetime
    undone_at: datetime | None = None
    # trash(휴지통) · spam(스팸함) · file(보관함). 되돌릴 때 어디서 꺼낼지 정한다.
    action: str = "trash"
    # 보관함으로 옮겼으면 그 라벨
    label_id: str = ""


@dataclass(frozen=True, slots=True)
class WaitingReply:
    id: int
    account: str
    thread_id: str
    message_id: str
    subject: str
    sender: str
    created_at: datetime
    due_at: datetime | None = None
    resolved_at: datetime | None = None
    reminded_at: datetime | None = None


class MailRuleRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    async def add(
        self,
        name: str,
        kind: str,
        now: datetime,
        *,
        senders: tuple[str, ...] = (),
        domains: tuple[str, ...] = (),
        keywords: tuple[str, ...] = (),
        account: str = "",
    ) -> MailRule:
        cursor = await self._conn.execute(
            """
            INSERT INTO mail_rules (name, kind, senders, domains, keywords, account, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                kind,
                json.dumps(list(senders), ensure_ascii=False),
                json.dumps(list(domains), ensure_ascii=False),
                json.dumps(list(keywords), ensure_ascii=False),
                account,
                to_db_time(now),
            ),
        )
        await self._conn.commit()
        rule = await self.get(cursor.lastrowid)
        assert rule is not None
        return rule

    async def get(self, rule_id: int) -> MailRule | None:
        async with self._conn.execute("SELECT * FROM mail_rules WHERE id = ?", (rule_id,)) as cursor:
            row = await cursor.fetchone()
        return _rule(row) if row else None

    async def list_all(self) -> list[MailRule]:
        async with self._conn.execute("SELECT * FROM mail_rules ORDER BY id") as cursor:
            rows = await cursor.fetchall()
        return [_rule(row) for row in rows]

    async def delete(self, rule_id: int) -> bool:
        cursor = await self._conn.execute("DELETE FROM mail_rules WHERE id = ?", (rule_id,))
        await self._conn.commit()
        return cursor.rowcount > 0

    async def set_enabled(self, rule_id: int, enabled: bool) -> bool:
        cursor = await self._conn.execute(
            "UPDATE mail_rules SET enabled = ? WHERE id = ?", (1 if enabled else 0, rule_id)
        )
        await self._conn.commit()
        return cursor.rowcount > 0


class MailStateStore:
    """계정별 수집 커서와 이미 본 메일."""

    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    async def history_id(self, account: str) -> str | None:
        async with self._conn.execute("SELECT history_id FROM mail_state WHERE account = ?", (account,)) as cursor:
            row = await cursor.fetchone()
        return row["history_id"] if row else None

    async def save_history_id(self, account: str, history_id: str, now: datetime) -> None:
        await self._conn.execute(
            """
            INSERT INTO mail_state (account, history_id, checked_at) VALUES (?, ?, ?)
            ON CONFLICT (account) DO UPDATE SET history_id = excluded.history_id, checked_at = excluded.checked_at
            """,
            (account, history_id, to_db_time(now)),
        )
        await self._conn.commit()

    async def last_checked(self, account: str) -> datetime | None:
        async with self._conn.execute("SELECT checked_at FROM mail_state WHERE account = ?", (account,)) as cursor:
            row = await cursor.fetchone()
        return from_db_time(row["checked_at"]) if row and row["checked_at"] else None

    async def mark_seen(self, account: str, message_id: str, kind: str, subject: str, now: datetime) -> bool:
        """처음 보는 메일이면 True. 같은 메일을 두 번 처리하지 않는다."""
        cursor = await self._conn.execute(
            """
            INSERT OR IGNORE INTO mail_seen (account, message_id, kind, subject, seen_at, briefed)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (account, message_id, kind, subject, to_db_time(now)),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def unbriefed(self, *kinds: str) -> list[tuple[str, str]]:
        """아직 브리핑에 넣지 않은 메일 (계정, 제목)."""
        marks = ", ".join("?" for _ in kinds)
        async with self._conn.execute(
            f"SELECT account, subject FROM mail_seen WHERE kind IN ({marks}) AND briefed = 0 ORDER BY seen_at", kinds
        ) as cursor:
            rows = await cursor.fetchall()
        return [(row["account"], row["subject"]) for row in rows]

    async def mark_briefed(self, *kinds: str) -> None:
        marks = ", ".join("?" for _ in kinds)
        await self._conn.execute(f"UPDATE mail_seen SET briefed = 1 WHERE kind IN ({marks}) AND briefed = 0", kinds)
        await self._conn.commit()


class MailCleanupLog:
    """규칙 엔진이 휴지통·스팸함·보관함으로 옮긴 내역. 저녁 브리핑의 [되돌리기]가 이 표를 쓴다."""

    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    async def record(
        self,
        account: str,
        message_id: str,
        subject: str,
        sender: str,
        now: datetime,
        action: str = "trash",
        label_id: str = "",
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO mail_cleanup (account, message_id, subject, sender, done_at, action, label_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (account, message_id, subject, sender, to_db_time(now), action, label_id),
        )
        await self._conn.commit()

    async def since(self, start: datetime) -> list[CleanupRecord]:
        async with self._conn.execute(
            "SELECT * FROM mail_cleanup WHERE done_at >= ? AND undone_at IS NULL ORDER BY id",
            (to_db_time(start),),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_cleanup(row) for row in rows]

    async def mark_undone(self, record_id: int, now: datetime) -> bool:
        cursor = await self._conn.execute(
            "UPDATE mail_cleanup SET undone_at = ? WHERE id = ? AND undone_at IS NULL", (to_db_time(now), record_id)
        )
        await self._conn.commit()
        return cursor.rowcount > 0


class WaitingReplyStore:
    """답변 대기 (Inbox Zero의 Reply Zero 참고). 답장하면 자동 해제한다."""

    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    async def add(
        self,
        account: str,
        thread_id: str,
        message_id: str,
        subject: str,
        sender: str,
        now: datetime,
        due_at: datetime | None = None,
    ) -> WaitingReply:
        cursor = await self._conn.execute(
            """
            INSERT OR IGNORE INTO reply_waiting (account, thread_id, message_id, subject, sender, created_at, due_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (account, thread_id, message_id, subject, sender, to_db_time(now), to_db_time(due_at)),
        )
        await self._conn.commit()
        if cursor.rowcount == 0:
            existing = await self.by_thread(account, thread_id)
            assert existing is not None
            return existing
        waiting = await self.get(cursor.lastrowid)
        assert waiting is not None
        return waiting

    async def get(self, waiting_id: int) -> WaitingReply | None:
        async with self._conn.execute("SELECT * FROM reply_waiting WHERE id = ?", (waiting_id,)) as cursor:
            row = await cursor.fetchone()
        return _waiting(row) if row else None

    async def by_thread(self, account: str, thread_id: str) -> WaitingReply | None:
        async with self._conn.execute(
            "SELECT * FROM reply_waiting WHERE account = ? AND thread_id = ?", (account, thread_id)
        ) as cursor:
            row = await cursor.fetchone()
        return _waiting(row) if row else None

    async def open_items(self) -> list[WaitingReply]:
        async with self._conn.execute(
            "SELECT * FROM reply_waiting WHERE resolved_at IS NULL ORDER BY created_at"
        ) as cursor:
            rows = await cursor.fetchall()
        return [_waiting(row) for row in rows]

    async def resolve(self, waiting_id: int, now: datetime) -> bool:
        cursor = await self._conn.execute(
            "UPDATE reply_waiting SET resolved_at = ? WHERE id = ? AND resolved_at IS NULL",
            (to_db_time(now), waiting_id),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def mark_reminded(self, waiting_id: int, now: datetime) -> None:
        await self._conn.execute(
            "UPDATE reply_waiting SET reminded_at = ? WHERE id = ?", (to_db_time(now), waiting_id)
        )
        await self._conn.commit()


def _rule(row: aiosqlite.Row) -> MailRule:
    return MailRule(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        senders=tuple(json.loads(row["senders"] or "[]")),
        domains=tuple(json.loads(row["domains"] or "[]")),
        keywords=tuple(json.loads(row["keywords"] or "[]")),
        account=row["account"] or "",
        enabled=bool(row["enabled"]),
    )


def _cleanup(row: aiosqlite.Row) -> CleanupRecord:
    return CleanupRecord(
        id=row["id"],
        account=row["account"],
        message_id=row["message_id"],
        subject=row["subject"],
        sender=row["sender"],
        done_at=from_db_time(row["done_at"]),
        undone_at=from_db_time(row["undone_at"]),
        action=row["action"],
        label_id=row["label_id"],
    )


def _waiting(row: aiosqlite.Row) -> WaitingReply:
    return WaitingReply(
        id=row["id"],
        account=row["account"],
        thread_id=row["thread_id"],
        message_id=row["message_id"],
        subject=row["subject"],
        sender=row["sender"],
        created_at=from_db_time(row["created_at"]),
        due_at=from_db_time(row["due_at"]),
        resolved_at=from_db_time(row["resolved_at"]),
        reminded_at=from_db_time(row["reminded_at"]),
    )
