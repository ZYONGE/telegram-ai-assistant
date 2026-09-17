"""할 일 저장소. eClass 과제, 메일 후속 동작, 대화로 추가한 할 일이 모두 여기로 모인다."""

from dataclasses import dataclass
from datetime import datetime

import aiosqlite

from app.core.events import Event
from app.storage.db import Database, from_db_time, to_db_time

_UNSET = object()


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

    async def add(
        self,
        title: str,
        now: datetime,
        *,
        notes: str = "",
        due_at: datetime | None = None,
        source: str = "chat",
    ) -> Todo:
        stamp = to_db_time(now)
        cursor = await self._conn.execute(
            """
            INSERT INTO todos (title, notes, due_at, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (title, notes, to_db_time(due_at), source, stamp, stamp),
        )
        await self._conn.commit()
        todo = await self.get(cursor.lastrowid)
        assert todo is not None
        return todo

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

    async def get(self, todo_id: int) -> Todo | None:
        return await self._one("SELECT * FROM todos WHERE id = ?", (todo_id,))

    async def get_by_ref(self, ref_id: str) -> Todo | None:
        return await self._one("SELECT * FROM todos WHERE ref_id = ?", (ref_id,))

    async def update(
        self,
        todo_id: int,
        now: datetime,
        *,
        title: str | None = None,
        notes: str | None = None,
        due_at: datetime | None | object = _UNSET,
    ) -> Todo | None:
        current = await self.get(todo_id)
        if current is None:
            return None
        new_due = current.due_at if due_at is _UNSET else due_at
        await self._conn.execute(
            "UPDATE todos SET title = ?, notes = ?, due_at = ?, updated_at = ? WHERE id = ?",
            (
                current.title if title is None else title,
                current.notes if notes is None else notes,
                to_db_time(new_due),  # type: ignore[arg-type]
                to_db_time(now),
                todo_id,
            ),
        )
        await self._conn.commit()
        return await self.get(todo_id)

    async def set_status(self, todo_id: int, status: str, now: datetime) -> Todo | None:
        await self._conn.execute(
            "UPDATE todos SET status = ?, updated_at = ? WHERE id = ?",
            (status, to_db_time(now), todo_id),
        )
        await self._conn.commit()
        return await self.get(todo_id)

    async def delete(self, todo_id: int) -> bool:
        cursor = await self._conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
        await self._conn.commit()
        return cursor.rowcount == 1

    async def list_open(self) -> list[Todo]:
        return await self._many("SELECT * FROM todos WHERE status = 'open' ORDER BY due_at IS NULL, due_at, id")

    async def list_done_between(self, start: datetime, end: datetime) -> list[Todo]:
        return await self._many(
            "SELECT * FROM todos WHERE status = 'done' AND updated_at >= ? AND updated_at < ? ORDER BY updated_at",
            (to_db_time(start), to_db_time(end)),
        )

    async def _one(self, sql: str, params: tuple) -> Todo | None:
        async with self._conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        return _to_todo(row) if row else None

    async def _many(self, sql: str, params: tuple = ()) -> list[Todo]:
        async with self._conn.execute(sql, params) as cursor:
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
