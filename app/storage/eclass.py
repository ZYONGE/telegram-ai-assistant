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
# 한 번에 돌려주는 검색 결과 수
SEARCH_LIMIT = 10
# 낱말을 너무 많이 넣으면 아무것도 안 걸린다
MAX_WORDS = 5

# 주기 판정의 여유. 예약이 몇 초 밀렸다고 다음 주기까지 통째로 건너뛰지 않게 한다.
DUE_TOLERANCE = timedelta(minutes=1)


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
    # 글 본문. 외부에서 온 글이라 저장만 하고 지시로 다루지 않는다 (절대 규칙 8).
    body: str = ""
    # 글이 올라온 시각. 마감(due_at)과 다르다.
    posted_at: datetime | None = None
    # 어느 화면에서 왔는지 (수집 소스 이름)
    source: str = ""


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
    body: str = ""
    posted_at: datetime | None = None
    source: str = ""


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
                INSERT INTO eclass_items
                    (item_id, kind, course, title, due_at, url, body, posted_at, source, first_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.item_id,
                    item.kind,
                    item.course,
                    item.title,
                    to_db_time(item.due_at),
                    item.url,
                    item.body,
                    to_db_time(item.posted_at),
                    item.source,
                    stamp,
                    stamp,
                ),
            )
            await self._conn.commit()
            return ItemChange.NEW

        changed = previous.due_at != item.due_at
        await self._conn.execute(
            """
            UPDATE eclass_items SET kind = ?, course = ?, title = ?, due_at = ?, url = ?,
                body = ?, posted_at = ?, source = ?, updated_at = ?
            WHERE item_id = ?
            """,
            (
                item.kind,
                item.course,
                item.title,
                to_db_time(item.due_at),
                item.url,
                # 본문을 늦게 읽어 온 경우에만 채운다. 이미 있는 본문을 빈 값으로 지우지 않는다.
                item.body or previous.body,
                to_db_time(item.posted_at or previous.posted_at),
                item.source or previous.source,
                stamp,
                item.item_id,
            ),
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

    async def search(
        self,
        query: str = "",
        course: str = "",
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = SEARCH_LIMIT,
    ) -> list[StoredItem]:
        """제목·본문·과목에서 낱말을 **모두** 포함하는 글을 최근 순으로 찾는다.

        기간은 글이 올라온 시각을 먼저 보고, 없으면 처음 본 시각으로 대신한다
        (올라온 시각을 못 읽는 화면이 있다).
        """
        where = []
        params: list = []
        for word in [word for word in query.split() if word][:MAX_WORDS]:
            where.append("lower(title || ' ' || body || ' ' || course) LIKE ?")
            params.append(f"%{word.lower()}%")
        if course:
            where.append("lower(course) LIKE ?")
            params.append(f"%{course.lower().strip()}%")
        if since is not None:
            where.append("COALESCE(posted_at, first_seen_at) >= ?")
            params.append(to_db_time(since))
        if until is not None:
            where.append("COALESCE(posted_at, first_seen_at) < ?")
            params.append(to_db_time(until))

        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)
        async with self._conn.execute(
            f"""
            SELECT * FROM eclass_items {clause}
            ORDER BY COALESCE(posted_at, first_seen_at) DESC, item_id DESC LIMIT ?
            """,
            tuple(params),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_item(row) for row in rows]

    async def courses(self) -> list[str]:
        """기억하고 있는 과목 이름. 사용자가 말한 과목을 찾아 줄 때 쓴다."""
        async with self._conn.execute(
            "SELECT DISTINCT course FROM eclass_items WHERE course <> '' ORDER BY course"
        ) as cursor:
            rows = await cursor.fetchall()
        return [row["course"] for row in rows]

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
        body=row["body"] or "",
        posted_at=from_db_time(row["posted_at"]),
        source=row["source"] or "",
    )


@dataclass(frozen=True, slots=True)
class SourceState:
    """수집 소스 하나의 마지막 수행 기록."""

    source: str
    last_run_at: datetime
    last_ok_at: datetime | None = None
    last_reason: str = ""


class EclassSourceStateStore:
    """소스별 수집 주기를 관리한다.

    수집기는 예약된 주기마다 깨어나지만, 소스마다 볼 간격이 다르다
    (할 일은 매번, 강의계획서는 하루 한 번). 마지막으로 돌린 시각을 기억해
    주기가 된 소스만 돌린다.
    """

    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    async def due(self, source: str, interval: timedelta, now: datetime) -> bool:
        """이 소스를 지금 돌릴 차례인지. 한 번도 안 돌렸으면 돌린다."""
        state = await self.read(source)
        if state is None:
            return True
        # 예약 시각이 몇 초씩 밀리는 것 때문에 한 주기를 통째로 건너뛰지 않도록 여유를 둔다
        return now - state.last_run_at >= interval - DUE_TOLERANCE

    async def record_run(self, source: str, now: datetime, ok: bool, reason: str = "") -> None:
        previous = await self.read(source)
        last_ok = now if ok else (previous.last_ok_at if previous else None)
        await self._conn.execute(
            """
            INSERT INTO eclass_source_state (source, last_run_at, last_ok_at, last_reason) VALUES (?, ?, ?, ?)
            ON CONFLICT (source) DO UPDATE SET last_run_at = excluded.last_run_at,
                last_ok_at = excluded.last_ok_at, last_reason = excluded.last_reason
            """,
            (source, to_db_time(now), to_db_time(last_ok), "" if ok else reason),
        )
        await self._conn.commit()

    async def read(self, source: str) -> SourceState | None:
        async with self._conn.execute(
            "SELECT * FROM eclass_source_state WHERE source = ?", (source,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return SourceState(
            source=row["source"],
            last_run_at=from_db_time(row["last_run_at"]),
            last_ok_at=from_db_time(row["last_ok_at"]),
            last_reason=row["last_reason"] or "",
        )
