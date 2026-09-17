"""알림 게이트 결정 기록."""

import json
from dataclasses import dataclass
from datetime import datetime

import aiosqlite

from app.core.events import Event
from app.core.interfaces import GateAction, GateDecision
from app.storage.db import Database, from_db_time, to_db_time


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    event: Event
    action: GateAction
    reason: str
    release_at: datetime | None
    decided_at: datetime
    sent_at: datetime | None


class NotificationLog:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    async def get(self, ref_id: str) -> NotificationRecord | None:
        async with self._conn.execute("SELECT * FROM notifications WHERE ref_id = ?", (ref_id,)) as cursor:
            row = await cursor.fetchone()
        return _to_record(row) if row else None

    async def save_decision(self, event: Event, decision: GateDecision, now: datetime) -> None:
        if decision.action is GateAction.DROP:
            raise ValueError("DROP 결정은 기록하지 않습니다")
        await self._conn.execute(
            """
            INSERT INTO notifications
                (ref_id, source, kind, title, body, urgent, user_requested, due_at, meta,
                 action, reason, release_at, decided_at, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT (ref_id) DO UPDATE SET
                action = excluded.action,
                reason = excluded.reason,
                release_at = excluded.release_at,
                decided_at = excluded.decided_at,
                sent_at = NULL
            """,
            (
                event.ref_id,
                event.source,
                event.kind,
                event.title,
                event.body,
                int(event.urgent),
                int(event.user_requested),
                to_db_time(event.due_at),
                json.dumps(event.meta, ensure_ascii=False),
                decision.action.value,
                decision.reason,
                to_db_time(decision.release_at),
                to_db_time(now),
            ),
        )
        await self._conn.commit()

    async def mark_sent(self, ref_id: str, at: datetime) -> None:
        await self._conn.execute("UPDATE notifications SET sent_at = ? WHERE ref_id = ?", (to_db_time(at), ref_id))
        await self._conn.commit()

    async def count_sent_since(self, since: datetime) -> int:
        """일일 상한 계산용. 사용자가 요청한 알림은 세지 않는다."""
        async with self._conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_requested = 0 AND sent_at >= ?",
            (to_db_time(since),),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0])

    async def pending_delivery(self, now: datetime) -> list[NotificationRecord]:
        """보류가 풀린 알림과, 즉시 발송하기로 했지만 아직 보내지 못한 알림."""
        async with self._conn.execute(
            """
            SELECT * FROM notifications
            WHERE sent_at IS NULL
              AND (action = 'send_now' OR (action = 'hold' AND release_at <= ?))
            ORDER BY decided_at, ref_id
            """,
            (to_db_time(now),),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_to_record(row) for row in rows]


def _to_record(row: aiosqlite.Row) -> NotificationRecord:
    event = Event(
        source=row["source"],
        kind=row["kind"],
        title=row["title"],
        ref_id=row["ref_id"],
        body=row["body"],
        urgent=bool(row["urgent"]),
        user_requested=bool(row["user_requested"]),
        due_at=from_db_time(row["due_at"]),
        meta=json.loads(row["meta"]),
    )
    return NotificationRecord(
        event=event,
        action=GateAction(row["action"]),
        reason=row["reason"],
        release_at=from_db_time(row["release_at"]),
        decided_at=from_db_time(row["decided_at"]),
        sent_at=from_db_time(row["sent_at"]),
    )
