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
    """
    -- 브리핑에 포함된 시각. batch 결정을 받은 소식은 다음 브리핑에 한 번만 들어간다.
    ALTER TABLE notifications ADD COLUMN briefed_at TEXT;

    -- 대화로 등록한 리마인더·예약 작업. 실행 시각 계산은 APScheduler가 하고, 이 표가 원본이다.
    CREATE TABLE scheduled_tasks (
        id          TEXT PRIMARY KEY,
        kind        TEXT NOT NULL CHECK (kind IN ('reminder', 'agent')),
        content     TEXT NOT NULL,
        run_at      TEXT,
        cron        TEXT,
        status      TEXT NOT NULL CHECK (status IN ('active', 'paused', 'completed', 'cancelled')),
        fail_count  INTEGER NOT NULL DEFAULT 0,
        last_run_at TEXT,
        created_at  TEXT NOT NULL,
        CHECK ((run_at IS NULL) <> (cron IS NULL))
    );

    CREATE TABLE task_runs (
        id       INTEGER PRIMARY KEY,
        task_id  TEXT NOT NULL REFERENCES scheduled_tasks (id),
        ran_at   TEXT NOT NULL,
        ok       INTEGER NOT NULL,
        detail   TEXT NOT NULL
    );

    -- 대화 기록. content는 API 메시지 content 블록 목록(JSON)이다.
    CREATE TABLE conversation_messages (
        id          INTEGER PRIMARY KEY,
        role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        content     TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        archived    INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE conversation_state (
        id                  INTEGER PRIMARY KEY CHECK (id = 1),
        summary             TEXT NOT NULL DEFAULT '',
        summary_updated_at  TEXT
    );

    -- 다음 사용자 메시지에 덧붙일 알림 (확인 버튼 처리 결과 등)
    CREATE TABLE conversation_notes (
        id          INTEGER PRIMARY KEY,
        text        TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        consumed    INTEGER NOT NULL DEFAULT 0
    );

    -- 확인 버튼을 기다리는 도구 호출
    CREATE TABLE pending_actions (
        id           TEXT PRIMARY KEY,
        tool_name    TEXT NOT NULL,
        args         TEXT NOT NULL,
        summary      TEXT NOT NULL,
        status       TEXT NOT NULL CHECK (status IN ('pending', 'done', 'cancelled')),
        created_at   TEXT NOT NULL,
        resolved_at  TEXT
    );
    """,
    """
    -- 모델 제공자 변경(Claude → Gemini)으로 대화 기록 형식이 바뀌어, 이전 기록은 다시 보내지 않는다.
    UPDATE conversation_messages SET archived = 1 WHERE archived = 0;
    """,
    """
    -- 보관함: 링크 요약과 텍스트 메모를 한 표에 모은다 (사진·음성은 저장하지 않는다).
    CREATE TABLE archive_items (
        id          INTEGER PRIMARY KEY,
        kind        TEXT NOT NULL CHECK (kind IN ('link', 'note')),
        title       TEXT NOT NULL,
        url         TEXT NOT NULL DEFAULT '',
        summary     TEXT NOT NULL DEFAULT '',
        body        TEXT NOT NULL DEFAULT '',
        tags        TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL
    );
    CREATE INDEX idx_archive_created ON archive_items (created_at);
    """,
    """
    -- 텔레그램으로 받은 마지막 위치 (한 줄만 유지). 좌표는 개인정보라 이 DB 밖으로 내보내지 않는다.
    CREATE TABLE user_location (
        id          INTEGER PRIMARY KEY CHECK (id = 1),
        lat         REAL NOT NULL,
        lon         REAL NOT NULL,
        updated_at  TEXT NOT NULL,
        live_until  TEXT
    );
    """,
    """
    -- 사용자가 등록한 메일 유형. 매칭은 코드가 하고, 모델은 이 목록 밖으로 나가지 않는다.
    CREATE TABLE mail_rules (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL,
        kind        TEXT NOT NULL CHECK (kind IN ('payment', 'professor', 'company', 'ad', 'other')),
        senders     TEXT NOT NULL DEFAULT '[]',
        domains     TEXT NOT NULL DEFAULT '[]',
        keywords    TEXT NOT NULL DEFAULT '[]',
        account     TEXT NOT NULL DEFAULT '',
        enabled     INTEGER NOT NULL DEFAULT 1,
        created_at  TEXT NOT NULL
    );

    -- 계정별 수집 커서
    CREATE TABLE mail_state (
        account     TEXT PRIMARY KEY,
        history_id  TEXT,
        checked_at  TEXT
    );

    -- 이미 처리한 메일 (중복 방지 + 브리핑 목록). 본문은 저장하지 않는다.
    CREATE TABLE mail_seen (
        account     TEXT NOT NULL,
        message_id  TEXT NOT NULL,
        kind        TEXT NOT NULL,
        subject     TEXT NOT NULL DEFAULT '',
        seen_at     TEXT NOT NULL,
        briefed     INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (account, message_id)
    );

    -- 규칙 엔진이 휴지통으로 보낸 내역 ([되돌리기] 대상)
    CREATE TABLE mail_cleanup (
        id          INTEGER PRIMARY KEY,
        account     TEXT NOT NULL,
        message_id  TEXT NOT NULL,
        subject     TEXT NOT NULL DEFAULT '',
        sender      TEXT NOT NULL DEFAULT '',
        done_at     TEXT NOT NULL,
        undone_at   TEXT
    );

    -- 답변 대기. 사용자가 그 스레드에 답장하면 자동으로 풀린다.
    CREATE TABLE reply_waiting (
        id           INTEGER PRIMARY KEY,
        account      TEXT NOT NULL,
        thread_id    TEXT NOT NULL,
        message_id   TEXT NOT NULL DEFAULT '',
        subject      TEXT NOT NULL DEFAULT '',
        sender       TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL,
        due_at       TEXT,
        resolved_at  TEXT,
        reminded_at  TEXT,
        UNIQUE (account, thread_id)
    );
    """,
    """
    -- eClass에서 본 글과 과제. 마감 변경을 알아채려고 이전 마감 값을 남긴다.
    CREATE TABLE eclass_items (
        item_id        TEXT PRIMARY KEY,
        kind           TEXT NOT NULL,
        course         TEXT NOT NULL DEFAULT '',
        title          TEXT NOT NULL,
        due_at         TEXT,
        url            TEXT NOT NULL DEFAULT '',
        first_seen_at  TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    );
    CREATE INDEX idx_eclass_due ON eclass_items (due_at);

    -- 수집 성공·실패 이력 (한 줄). 로그인 연속 실패를 세어 자동화를 멈추는 근거가 된다.
    CREATE TABLE eclass_state (
        id           INTEGER PRIMARY KEY CHECK (id = 1),
        last_ok_at   TEXT,
        fail_count   INTEGER NOT NULL DEFAULT 0,
        last_reason  TEXT NOT NULL DEFAULT '',
        failed_at    TEXT
    );
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
