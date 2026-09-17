"""SQLite 연결과 스키마 마이그레이션. 모든 기능이 이 DB 파일 하나를 공유한다."""

from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from app.core.clock import require_aware

# 순서대로 한 번씩 적용한다. 적용된 개수는 PRAGMA user_version에 기록된다.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE todos (
        id          INTEGER PRIMARY KEY,
        title       TEXT NOT NULL,
        notes       TEXT NOT NULL DEFAULT '',
        due_at      TEXT,
        status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
        source      TEXT NOT NULL,
        ref_id      TEXT UNIQUE,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );

    -- 알림 게이트를 거친 모든 이벤트와 그 결정. 중복 방지, 보류 해제, 일일 상한의 근거.
    CREATE TABLE notifications (
        ref_id          TEXT PRIMARY KEY,
        source          TEXT NOT NULL,
        kind            TEXT NOT NULL,
        title           TEXT NOT NULL,
        body            TEXT NOT NULL,
        urgent          INTEGER NOT NULL,
        user_requested  INTEGER NOT NULL,
        due_at          TEXT,
        meta            TEXT NOT NULL,
        action          TEXT NOT NULL CHECK (action IN ('send_now', 'batch', 'hold')),
        reason          TEXT NOT NULL,
        release_at      TEXT,
        decided_at      TEXT NOT NULL,
        sent_at         TEXT
    );
    CREATE INDEX idx_notifications_pending ON notifications (action, sent_at, release_at);
    """,
]


def to_db_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    # 자릿수를 고정해야 SQL에서 문자열 비교로 시각 순서를 비교할 수 있다
    return require_aware(value, "value").astimezone(UTC).isoformat(timespec="microseconds")


def from_db_time(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


class Database:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    @classmethod
    async def open(cls, path: Path | str) -> "Database":
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        db = cls(conn)
        await db._migrate()
        return db

    async def schema_version(self) -> int:
        async with self.conn.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        return int(row[0])

    async def _migrate(self) -> None:
        current = await self.schema_version()
        for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
            await self.conn.executescript(script)
            await self.conn.execute(f"PRAGMA user_version = {version}")
            await self.conn.commit()

    async def close(self) -> None:
        await self.conn.close()
