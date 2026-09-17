"""할 일 저장소. eClass 과제, 메일 후속 동작, 대화로 추가한 할 일이 모두 여기로 모인다."""

from dataclasses import dataclass
from datetime import datetime

import aiosqlite

from app.core.events import Event
from app.storage.db import Database, from_db_time, to_db_time


@dataclass(frozen=True, slots=True)
class Todo:
    id: int
    title: str
    notes: str
    due_at: datetime | None
    status: str
    source: str
    ref_id: str | None
    created_at: datetime
    updated_at: datetime


class TodoRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    async def add_from_event(self, event: Event, now: datetime) -> tuple[Todo, bool]:
        """이벤트로 할 일을 등록한다. 같은 ref_id가 이미 있으면 새로 만들지 않는다.

        반환값: (할 일, 이번에 새로 만들었는지)
        """
        stamp = to_db_time(now)
        cursor = await self._conn.execute(
            """
            INSERT INTO todos (title, notes, due_at, source, ref_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (ref_id) DO NOTHING
            """,
            (event.title, event.body, to_db_time(event.due_at), event.source, event.ref_id, stamp, stamp),
        )
        created = cursor.rowcount == 1
        await self._conn.commit()
        todo = await self.get_by_ref(event.ref_id)
        assert todo is not None
        return todo, created

    async def get_by_ref(self, ref_id: str) -> Todo | None:
        async with self._conn.execute("SELECT * FROM todos WHERE ref_id = ?", (ref_id,)) as cursor:
            row = await cursor.fetchone()
        return _to_todo(row) if row else None

    async def list_open(self) -> list[Todo]:
        async with self._conn.execute(
            "SELECT * FROM todos WHERE status = 'open' ORDER BY due_at IS NULL, due_at, id"
        ) as cursor:
            rows = await cursor.fetchall()
        return [_to_todo(row) for row in rows]


def _to_todo(row: aiosqlite.Row) -> Todo:
    return Todo(
        id=row["id"],
        title=row["title"],
        notes=row["notes"],
        due_at=from_db_time(row["due_at"]),
        status=row["status"],
        source=row["source"],
        ref_id=row["ref_id"],
        created_at=from_db_time(row["created_at"]),
        updated_at=from_db_time(row["updated_at"]),
    )
