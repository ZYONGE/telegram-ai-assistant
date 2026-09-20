"""야간 백업: 잃으면 되돌릴 수 없는 것만 매일 복사해 둔다.

- **DB는 온라인 백업으로 뜬다.** 봇이 켜진 채로 파일을 복사하면 쓰는 도중에 걸려 깨진 사본이 남는다.
  SQLite가 제공하는 방식으로 뜨면 그런 일이 없다.
- 사람이 직접 쓴 글(기억·프로필·지시·개인 설정)과 수집 범위 결정도 함께 뜬다.
- **비밀값(.env)과 계정 토큰은 뜨지 않는다.** 잃어도 다시 발급받으면 되지만, 사본이 늘면 새어 나갈
  구멍도 늘어난다 (절대 규칙 6·12).
- 실패하면 조용히 넘어가지 않고 `collector_failed` 이벤트로 알린다. 백업이 멈춘 줄 모르는 것이
  백업이 없는 것보다 나쁘다 (CLAUDE.md 4-1절).
"""

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite

from app.core.clock import format_kst, to_kst
from app.core.config import BackupSettings
from app.core.events import Event, EventKind
from app.storage.db import Database

logger = logging.getLogger(__name__)

# 사람이 직접 쓴 것과 비서가 정한 것. 잃으면 다시 만들기 번거롭다.
COPY_FILES = (
    "memory.md",
    "profile.md",
    "instructions.md",
    "local.toml",
    "eclass_scope.json",
    "eclass_catalog.json",
)
DB_NAME = "assistant.db"
# 폴더 이름. 날짜별로 하나씩 쌓인다.
STAMP = "%Y-%m-%d"


@dataclass(frozen=True, slots=True)
class BackupResult:
    directory: Path
    files: int
    removed: int


class BackupService:
    name = "backup"

    def __init__(self, db: Database, private_dir: Path, settings: BackupSettings) -> None:
        self._db = db
        self._private = private_dir
        self._settings = settings

    async def run(self, now: datetime) -> list[Event]:
        """하루치를 뜬다. 성공하면 알릴 것이 없고, 실패하면 이벤트 하나를 돌려준다."""
        try:
            result = await self._backup(now)
        except Exception as exc:
            # 메시지에 경로·비밀값이 섞일 수 있어 종류만 남긴다
            logger.exception("백업 실패")
            return [_failed(type(exc).__name__, now)]
        logger.info(
            "백업: 파일 %d개를 %s에 떴습니다 (오래된 %d개 정리)",
            result.files,
            result.directory.name,
            result.removed,
        )
        return []

    async def _backup(self, now: datetime) -> BackupResult:
        target = self._settings.directory / to_kst(now).strftime(STAMP)
        target.mkdir(parents=True, exist_ok=True)

        await self._copy_db(target / DB_NAME)
        files = 1
        for name in COPY_FILES:
            source = self._private / name
            if source.exists():
                shutil.copy2(source, target / name)
                files += 1
        return BackupResult(target, files, self._prune(now))

    async def _copy_db(self, target: Path) -> None:
        """봇이 켜진 채로도 안전하게 뜬다. 쓰는 도중이면 SQLite가 알아서 맞춰 준다."""
        destination = await aiosqlite.connect(target)
        try:
            await self._db.conn.backup(destination)
        finally:
            await destination.close()

    def _prune(self, now: datetime) -> int:
        """보관 기간이 지난 날짜 폴더를 지운다. 이름이 날짜가 아닌 것은 건드리지 않는다."""
        if self._settings.keep_days <= 0:
            return 0
        oldest = to_kst(now).date() - timedelta(days=self._settings.keep_days)
        removed = 0
        for folder in self._settings.directory.iterdir():
            if not folder.is_dir():
                continue
            try:
                day = datetime.strptime(folder.name, STAMP).date()
            except ValueError:
                continue
            if day < oldest:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        return removed

    def latest(self) -> Path | None:
        """가장 최근 백업 폴더. 되돌릴 때 사람이 찾아보라고 둔다."""
        if not self._settings.directory.exists():
            return None
        folders = sorted(
            (folder for folder in self._settings.directory.iterdir() if folder.is_dir()),
            key=lambda folder: folder.name,
        )
        return folders[-1] if folders else None


def _failed(reason: str, now: datetime) -> Event:
    return Event(
        source="backup",
        kind=EventKind.COLLECTOR_FAILED,
        title="백업이 되지 않았습니다",
        body=f"{format_kst(now)} 백업에 실패했습니다 ({reason}). 서버 디스크와 private 폴더 권한을 확인해 주세요.",
        # 같은 원인이 이어지면 게이트가 한 번만 알린다
        ref_id=f"backup:failed:{reason}",
        meta={"reason": reason},
    )
