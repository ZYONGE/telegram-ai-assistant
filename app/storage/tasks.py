"""리마인더·예약 작업 저장소 (NanoClaw의 작업 모델 참고, docs/adr/0002)."""

import secrets
from dataclasses import dataclass
from datetime import datetime

import aiosqlite

from app.storage.db import Database, from_db_time, to_db_time

ACTIVE, PAUSED, COMPLETED, CANCELLED = "active", "paused", "completed", "cancelled"


@dataclass(frozen=True, slots=True)
class ScheduledTask:
    id: str
    kind: str
    content: str
    run_at: datetime | None
    cron: str | None
    status: str
    fail_count: int
    last_run_at: datetime | None
    created_at: datetime

    @property
    def recurring(self) -> bool:
        return self.cron is not None


@dataclass(frozen=True, slots=True)
class TaskRun:
    task_id: str
    ran_at: datetime
    ok: bool
    detail: str


def new_task_id() -> str:
    return f"t-{secrets.token_hex(3)}"


class TaskRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    async def create(
        self,
        kind: str,
        content: str,
        now: datetime,
        *,
        run_at: datetime | None = None,
        cron: str | None = None,
    ) -> ScheduledTask:
        task_id = new_task_id()
        await self._conn.execute(
            """
            INSERT INTO scheduled_tasks (id, kind, content, run_at, cron, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, kind, content, to_db_time(run_at), cron, ACTIVE, to_db_time(now)),
        )
        await self._conn.commit()
        task = await self.get(task_id)
        assert task is not None
        return task

    async def get(self, task_id: str) -> ScheduledTask | None:
        async with self._conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()
        return _to_task(row) if row else None

    async def list(self, statuses: tuple[str, ...] = (ACTIVE, PAUSED)) -> list[ScheduledTask]:
        marks = ", ".join("?" for _ in statuses)
        async with self._conn.execute(
            f"SELECT * FROM scheduled_tasks WHERE status IN ({marks}) ORDER BY created_at, id", statuses
        ) as cursor:
            rows = await cursor.fetchall()
        return [_to_task(row) for row in rows]

    async def set_status(self, task_id: str, status: str, *, expected: tuple[str, ...]) -> bool:
        """현재 상태가 expected 중 하나일 때만 바꾼다."""
        marks = ", ".join("?" for _ in expected)
        cursor = await self._conn.execute(
            f"UPDATE scheduled_tasks SET status = ? WHERE id = ? AND status IN ({marks})",
            (status, task_id, *expected),
        )
        await self._conn.commit()
        return cursor.rowcount == 1

    async def record_run(self, task_id: str, ran_at: datetime, ok: bool, detail: str) -> int:
        """실행 결과를 남기고, 연속 실패 횟수를 반환한다."""
        await self._conn.execute(
            "INSERT INTO task_runs (task_id, ran_at, ok, detail) VALUES (?, ?, ?, ?)",
            (task_id, to_db_time(ran_at), int(ok), detail),
        )
        await self._conn.execute(
            """
            UPDATE scheduled_tasks
            SET last_run_at = ?, fail_count = CASE WHEN ? THEN 0 ELSE fail_count + 1 END
            WHERE id = ?
            """,
            (to_db_time(ran_at), int(ok), task_id),
        )
        await self._conn.commit()
        task = await self.get(task_id)
        return task.fail_count if task else 0

    async def runs(self, task_id: str, limit: int = 10) -> list[TaskRun]:
        async with self._conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT ?", (task_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
        return [TaskRun(row["task_id"], from_db_time(row["ran_at"]), bool(row["ok"]), row["detail"]) for row in rows]


def _to_task(row: aiosqlite.Row) -> ScheduledTask:
    return ScheduledTask(
        id=row["id"],
        kind=row["kind"],
        content=row["content"],
        run_at=from_db_time(row["run_at"]),
        cron=row["cron"],
        status=row["status"],
        fail_count=row["fail_count"],
        last_run_at=from_db_time(row["last_run_at"]),
        created_at=from_db_time(row["created_at"]),
    )
