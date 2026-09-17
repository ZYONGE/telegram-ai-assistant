"""대화 기록 저장소.

기록은 추가만 한다. 압축할 때는 지금까지의 기록 전체를 보관(archived) 처리하고
요약을 남겨, 다음 대화는 요약을 시스템 프롬프트에 담은 새 대화로 시작한다.
"""

import json
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.storage.db import Database, from_db_time, to_db_time

INTERRUPTED_RESULT = "이 도구 실행은 중단되어 결과가 없습니다."


@dataclass(frozen=True, slots=True)
class StoredMessage:
    id: int
    role: str
    content: list[dict[str, Any]]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PendingAction:
    id: str
    tool_name: str
    args: dict[str, Any]
    summary: str
    status: str


class ConversationStore:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    async def append(self, role: str, content: list[dict[str, Any]], now: datetime) -> None:
        await self._conn.execute(
            "INSERT INTO conversation_messages (role, content, created_at) VALUES (?, ?, ?)",
            (role, json.dumps(content, ensure_ascii=False), to_db_time(now)),
        )
        await self._conn.commit()

    async def active_messages(self, now: datetime) -> list[StoredMessage]:
        """보관되지 않은 대화. 마지막 도구 호출에 결과가 없으면(중단된 경우) 오류 결과를 채워 넣는다."""
        messages = await self._active()
        if messages and messages[-1].role == "assistant":
            dangling = [b["id"] for b in messages[-1].content if b.get("type") == "tool_use"]
            if dangling:
                results = [
                    {"type": "tool_result", "tool_use_id": tool_id, "content": INTERRUPTED_RESULT, "is_error": True}
                    for tool_id in dangling
                ]
                await self.append("user", results, now)
                messages = await self._active()
        return messages

    async def last_activity(self) -> datetime | None:
        async with self._conn.execute(
            "SELECT MAX(created_at) FROM conversation_messages WHERE archived = 0"
        ) as cursor:
            row = await cursor.fetchone()
        return from_db_time(row[0])

    async def count_active(self) -> int:
        async with self._conn.execute("SELECT COUNT(*) FROM conversation_messages WHERE archived = 0") as cursor:
            row = await cursor.fetchone()
        return int(row[0])

    async def summary(self) -> str:
        async with self._conn.execute("SELECT summary FROM conversation_state WHERE id = 1") as cursor:
            row = await cursor.fetchone()
        return row[0] if row else ""

    async def archive_with_summary(self, up_to_id: int, summary: str, now: datetime) -> None:
        await self._conn.execute(
            "UPDATE conversation_messages SET archived = 1 WHERE id <= ? AND archived = 0", (up_to_id,)
        )
        await self._conn.execute(
            """
            INSERT INTO conversation_state (id, summary, summary_updated_at) VALUES (1, ?, ?)
            ON CONFLICT (id) DO UPDATE SET summary = excluded.summary, summary_updated_at = excluded.summary_updated_at
            """,
            (summary, to_db_time(now)),
        )
        await self._conn.commit()

    async def add_note(self, text: str, now: datetime) -> None:
        await self._conn.execute(
            "INSERT INTO conversation_notes (text, created_at) VALUES (?, ?)", (text, to_db_time(now))
        )
        await self._conn.commit()

    async def consume_notes(self) -> list[str]:
        async with self._conn.execute(
            "SELECT id, text FROM conversation_notes WHERE consumed = 0 ORDER BY id"
        ) as cursor:
            rows = await cursor.fetchall()
        if rows:
            await self._conn.execute(
                f"UPDATE conversation_notes SET consumed = 1 WHERE id IN ({', '.join('?' for _ in rows)})",
                [row["id"] for row in rows],
            )
            await self._conn.commit()
        return [row["text"] for row in rows]

    async def _active(self) -> list[StoredMessage]:
        async with self._conn.execute(
            "SELECT * FROM conversation_messages WHERE archived = 0 ORDER BY id"
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            StoredMessage(row["id"], row["role"], json.loads(row["content"]), from_db_time(row["created_at"]))
            for row in rows
        ]


class PendingActionStore:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    async def create(self, tool_name: str, args: dict[str, Any], summary: str, now: datetime) -> PendingAction:
        action = PendingAction(f"a-{secrets.token_hex(4)}", tool_name, args, summary, "pending")
        await self._conn.execute(
            "INSERT INTO pending_actions (id, tool_name, args, summary, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (action.id, tool_name, json.dumps(args, ensure_ascii=False), summary, action.status, to_db_time(now)),
        )
        await self._conn.commit()
        return action

    async def get(self, action_id: str) -> PendingAction | None:
        async with self._conn.execute("SELECT * FROM pending_actions WHERE id = ?", (action_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return PendingAction(row["id"], row["tool_name"], json.loads(row["args"]), row["summary"], row["status"])

    async def resolve(self, action_id: str, status: str, now: datetime) -> bool:
        """대기 중인 작업만 처리한다. 버튼을 두 번 눌러도 한 번만 실행되게 한다."""
        cursor = await self._conn.execute(
            "UPDATE pending_actions SET status = ?, resolved_at = ? WHERE id = ? AND status = 'pending'",
            (status, to_db_time(now), action_id),
        )
        await self._conn.commit()
        return cursor.rowcount == 1
