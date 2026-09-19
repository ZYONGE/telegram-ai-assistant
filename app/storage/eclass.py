"""eClass 수집 상태 저장소.

- 본 글은 ID로 기억해 두 번 알리지 않는다.
- 과제 마감은 **이전 값을 남겨** 바뀌었는지 판단한다 (마감 변경 1회 알림).
- 수집 실패는 연속 횟수로 센다. 로그인 연속 2회 실패면 재시도를 멈춘다 (CLAUDE.md 6절).
- 학교 계정 비밀번호는 여기에 저장하지 않는다.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

import aiosqlite

from app.storage.db import Database, from_db_time, to_db_time

# 로그인 실패가 이만큼 이어지면 자동화를 멈춘다
MAX_LOGIN_FAILURES = 2


class ItemChange(StrEnum):
    NEW = "new"
    DUE_CHANGED = "due_changed"
    SAME = "same"


@dataclass(frozen=True, slots=True)
class EclassItem:
    item_id: str
    kind: str
    title: str
    course: str = ""
    due_at: datetime | None = None
    url: str = ""


@dataclass(frozen=True, slots=True)
class StoredItem:
    item_id: str
    kind: str
    title: str
    course: str
    due_at: datetime | None
    url: str
    first_seen_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CollectorHealth:
    last_ok_at: datetime | None
    fail_count: int
    last_reason: str

    def stale(self, now: datetime, hours: int) -> bool:
        """마지막 성공이 너무 오래됐는지. 한 번도 성공한 적 없으면 아직 판단하지 않는다."""
        return self.last_ok_at is not None and now - self.last_ok_at > timedelta(hours=hours)

    @property
    def login_blocked(self) -> bool:
        return self.last_reason == "login" and self.fail_count >= MAX_LOGIN_FAILURES


class EclassRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    async def upsert(self, item: EclassItem, now: datetime) -> ItemChange:
        """저장하고 무엇이 달라졌는지 알려 준다. 마감이 바뀌면 DUE_CHANGED."""
        previous = await self.get(item.item_id)
        stamp = to_db_time(now)
        if previous is None:
            await self._conn.execute(
                """
                INSERT INTO eclass_items (item_id, kind, course, title, due_at, url, first_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item.item_id, item.kind, item.course, item.title, to_db_time(item.due_at), item.url, stamp, stamp),
            )
            await self._conn.commit()
            return ItemChange.NEW

        changed = previous.due_at != item.due_at
        await self._conn.execute(
            """
            UPDATE eclass_items SET kind = ?, course = ?, title = ?, due_at = ?, url = ?, updated_at = ?
            WHERE item_id = ?
            """,
            (item.kind, item.course, item.title, to_db_time(item.due_at), item.url, stamp, item.item_id),
        )
        await self._conn.commit()
        return ItemChange.DUE_CHANGED if changed else ItemChange.SAME

    async def get(self, item_id: str) -> StoredItem | None:
        async with self._conn.execute("SELECT * FROM eclass_items WHERE item_id = ?", (item_id,)) as cursor:
            row = await cursor.fetchone()
        return _item(row) if row else None

    async def due_between(self, start: datetime, end: datetime) -> list[StoredItem]:
        async with self._conn.execute(
            "SELECT * FROM eclass_items WHERE due_at IS NOT NULL AND due_at >= ? AND due_at < ? ORDER BY due_at",
            (to_db_time(start), to_db_time(end)),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_item(row) for row in rows]

    async def count(self) -> int:
        async with self._conn.execute("SELECT COUNT(*) FROM eclass_items") as cursor:
            row = await cursor.fetchone()
        return int(row[0])


class EclassHealthStore:
    """수집 성공·실패 이력. 실패 종류(login, captcha, layout, network)를 구분해 센다."""

    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    async def record_success(self, now: datetime) -> None:
        await self._conn.execute(
            """
            INSERT INTO eclass_state (id, last_ok_at, fail_count, last_reason) VALUES (1, ?, 0, '')
            ON CONFLICT (id) DO UPDATE SET last_ok_at = excluded.last_ok_at, fail_count = 0, last_reason = ''
            """,
            (to_db_time(now),),
        )
        await self._conn.commit()

    async def record_failure(self, reason: str, now: datetime) -> int:
        """같은 이유가 이어지면 세고, 이유가 바뀌면 1부터 다시 센다. 연속 실패 횟수를 돌려준다."""
        health = await self.read()
        count = health.fail_count + 1 if health.last_reason == reason else 1
        await self._conn.execute(
            """
            INSERT INTO eclass_state (id, last_ok_at, fail_count, last_reason, failed_at) VALUES (1, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET fail_count = excluded.fail_count,
                last_reason = excluded.last_reason, failed_at = excluded.failed_at
            """,
            (to_db_time(health.last_ok_at), count, reason, to_db_time(now)),
        )
        await self._conn.commit()
        return count

    async def read(self) -> CollectorHealth:
        async with self._conn.execute("SELECT * FROM eclass_state WHERE id = 1") as cursor:
            row = await cursor.fetchone()
        if row is None:
            return CollectorHealth(None, 0, "")
        return CollectorHealth(
            last_ok_at=from_db_time(row["last_ok_at"]),
            fail_count=int(row["fail_count"]),
            last_reason=row["last_reason"] or "",
        )

    async def clear(self) -> None:
        """사용자가 비밀번호를 고친 뒤 다시 시도할 때 쓴다."""
        await self._conn.execute("UPDATE eclass_state SET fail_count = 0, last_reason = '' WHERE id = 1")
        await self._conn.commit()


def _item(row: aiosqlite.Row) -> StoredItem:
    return StoredItem(
        item_id=row["item_id"],
        kind=row["kind"],
        title=row["title"],
        course=row["course"] or "",
        due_at=from_db_time(row["due_at"]),
        url=row["url"] or "",
        first_seen_at=from_db_time(row["first_seen_at"]),
        updated_at=from_db_time(row["updated_at"]),
    )
