"""마지막으로 받은 위치.

좌표는 개인정보다. git에서 제외된 private/ 안의 DB에만 두고, 로그·프롬프트·이벤트에 남기지 않는다.
한 줄만 유지하며, 새 위치를 받으면 덮어쓴다.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.storage.db import Database, from_db_time, to_db_time


@dataclass(frozen=True, slots=True)
class StoredLocation:
    lat: float
    lon: float
    updated_at: datetime
    # 실시간 공유가 끝나는 시각 (한 번만 보낸 위치면 None)
    live_until: datetime | None = None

    def is_fresh(self, now: datetime, ttl: timedelta) -> bool:
        return now - self.updated_at <= ttl


class LocationStore:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    async def save(
        self, lat: float, lon: float, now: datetime, live_until: datetime | None = None
    ) -> StoredLocation:
        await self._conn.execute(
            """
            INSERT INTO user_location (id, lat, lon, updated_at, live_until) VALUES (1, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET lat = excluded.lat, lon = excluded.lon,
                updated_at = excluded.updated_at, live_until = excluded.live_until
            """,
            (float(lat), float(lon), to_db_time(now), to_db_time(live_until)),
        )
        await self._conn.commit()
        return StoredLocation(float(lat), float(lon), now, live_until)

    async def latest(self) -> StoredLocation | None:
        async with self._conn.execute("SELECT * FROM user_location WHERE id = 1") as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return StoredLocation(
            lat=row["lat"],
            lon=row["lon"],
            updated_at=from_db_time(row["updated_at"]),
            live_until=from_db_time(row["live_until"]),
        )

    async def clear(self) -> bool:
        cursor = await self._conn.execute("DELETE FROM user_location WHERE id = 1")
        await self._conn.commit()
        return cursor.rowcount > 0
