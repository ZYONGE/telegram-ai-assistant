"""보관함 저장소. 링크 요약과 텍스트 메모를 한 표에 모으고 말로 찾아본다."""

from dataclasses import dataclass
from datetime import datetime

import aiosqlite

from app.storage.db import Database, from_db_time, to_db_time

KINDS = ("link", "note")
SEARCH_LIMIT = 10


@dataclass(frozen=True, slots=True)
class ArchiveItem:
    id: int
    kind: str
    title: str
    url: str
    summary: str
    body: str
    tags: str
    created_at: datetime


class ArchiveRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    async def add(
        self,
        kind: str,
        title: str,
        now: datetime,
        *,
        url: str = "",
        summary: str = "",
        body: str = "",
        tags: str = "",
    ) -> ArchiveItem:
        if kind not in KINDS:
            raise ValueError(f"보관함 종류가 잘못되었습니다: {kind}")
        cursor = await self._conn.execute(
            """
            INSERT INTO archive_items (kind, title, url, summary, body, tags, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (kind, title, url, summary, body, tags, to_db_time(now)),
        )
        await self._conn.commit()
        item = await self.get(cursor.lastrowid)
        assert item is not None
        return item

    async def get(self, item_id: int) -> ArchiveItem | None:
        return await self._one("SELECT * FROM archive_items WHERE id = ?", (item_id,))

    async def find_by_url(self, url: str) -> ArchiveItem | None:
        return await self._one("SELECT * FROM archive_items WHERE url = ? ORDER BY id DESC LIMIT 1", (url,))

    async def search(self, query: str, limit: int = SEARCH_LIMIT) -> list[ArchiveItem]:
        """제목·요약·본문·태그에서 낱말을 모두 포함하는 항목을 최근 순으로 찾는다."""
        words = [word for word in query.split() if word][:5]
        if not words:
            return await self.list_recent(limit)
        haystack = "lower(title || ' ' || summary || ' ' || body || ' ' || tags)"
        where = " AND ".join(f"{haystack} LIKE ?" for _ in words)
        params = tuple(f"%{word.lower()}%" for word in words) + (limit,)
        return await self._many(f"SELECT * FROM archive_items WHERE {where} ORDER BY id DESC LIMIT ?", params)

    async def list_recent(self, limit: int = SEARCH_LIMIT) -> list[ArchiveItem]:
        return await self._many("SELECT * FROM archive_items ORDER BY id DESC LIMIT ?", (limit,))

    async def delete(self, item_id: int) -> bool:
        cursor = await self._conn.execute("DELETE FROM archive_items WHERE id = ?", (item_id,))
        await self._conn.commit()
        return cursor.rowcount > 0

    async def _one(self, sql: str, params: tuple) -> ArchiveItem | None:
        async with self._conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        return _item(row) if row else None

    async def _many(self, sql: str, params: tuple = ()) -> list[ArchiveItem]:
        async with self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [_item(row) for row in rows]


def _item(row: aiosqlite.Row) -> ArchiveItem:
    return ArchiveItem(
        id=row["id"],
        kind=row["kind"],
        title=row["title"],
        url=row["url"] or "",
        summary=row["summary"],
        body=row["body"],
        tags=row["tags"],
        created_at=from_db_time(row["created_at"]),
    )
