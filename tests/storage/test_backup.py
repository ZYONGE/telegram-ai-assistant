"""야간 백업 확인.

잃으면 되돌릴 수 없는 것만 뜨고, 비밀값은 뜨지 않는다.
백업이 멈춘 줄 모르는 것이 백업이 없는 것보다 나쁘므로 실패는 반드시 알린다.
"""

from datetime import time, timedelta

import aiosqlite
import pytest

from app.core.config import BackupSettings
from app.core.events import EventKind
from app.storage.backup import COPY_FILES, BackupService
from app.storage.todos import TodoRepository
from tests.conftest import kst

NOW = kst(9, 20, 3, 30)


@pytest.fixture
def private(tmp_path):
    folder = tmp_path / "private"
    folder.mkdir()
    for name in ("memory.md", "profile.md", "instructions.md", "local.toml", ".env"):
        (folder / name).write_text(f"{name} 내용", encoding="utf-8")
    (folder / "google_token_1.json").write_text("{}", encoding="utf-8")
    return folder


@pytest.fixture
def service(db, private):
    settings = BackupSettings(at=time(3, 30), keep_days=14, directory=private / "backups")
    return BackupService(db, private, settings)


async def test_a_backup_holds_the_database_and_what_we_wrote(service, private, db):
    await TodoRepository(db).add("과제 내기", NOW)

    assert await service.run(NOW) == []

    folder = private / "backups" / "2026-09-20"
    assert (folder / "assistant.db").exists()
    for name in ("memory.md", "profile.md", "instructions.md", "local.toml"):
        assert (folder / name).read_text(encoding="utf-8") == f"{name} 내용"


async def test_secrets_are_never_copied(service, private):
    """잃어도 다시 발급받으면 된다. 사본이 늘면 새어 나갈 구멍도 는다."""
    await service.run(NOW)

    folder = private / "backups" / "2026-09-20"
    assert not (folder / ".env").exists()
    assert not (folder / "google_token_1.json").exists()
    assert ".env" not in COPY_FILES


async def test_the_copied_database_can_be_opened_and_read(service, private, db):
    """봇이 켜진 채로 떠도 깨지지 않는다. 파일을 그냥 복사하면 그럴 수 없다."""
    await TodoRepository(db).add("과제 내기", NOW)
    await service.run(NOW)

    copied = await aiosqlite.connect(private / "backups" / "2026-09-20" / "assistant.db")
    try:
        async with copied.execute("SELECT title FROM todos") as cursor:
            rows = await cursor.fetchall()
    finally:
        await copied.close()
    assert [row[0] for row in rows] == ["과제 내기"]


async def test_a_file_that_does_not_exist_is_simply_skipped(service, private):
    (private / "instructions.md").unlink()
    assert await service.run(NOW) == []
    assert not (private / "backups" / "2026-09-20" / "instructions.md").exists()


async def test_running_twice_on_the_same_day_is_fine(service, private):
    await service.run(NOW)
    assert await service.run(NOW + timedelta(minutes=1)) == []


# --- 보관 기간 ---


async def test_old_backups_are_cleared_away(service, private):
    backups = private / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    (backups / "2026-08-01").mkdir()  # 50일 전
    (backups / "2026-09-18").mkdir()  # 이틀 전

    await service.run(NOW)

    assert not (backups / "2026-08-01").exists()
    assert (backups / "2026-09-18").exists()


async def test_something_that_is_not_a_backup_is_left_alone(service, private):
    backups = private / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    (backups / "메모").mkdir()
    (backups / "읽어주세요.txt").write_text("남겨 두세요", encoding="utf-8")

    await service.run(NOW)

    assert (backups / "메모").exists() and (backups / "읽어주세요.txt").exists()


async def test_keeping_forever_can_be_asked_for(db, private):
    settings = BackupSettings(keep_days=0, directory=private / "backups")
    (private / "backups").mkdir()
    (private / "backups" / "2020-01-01").mkdir()

    await BackupService(db, private, settings).run(NOW)
    assert (private / "backups" / "2020-01-01").exists()


async def test_the_newest_backup_can_be_found(service, private):
    await service.run(NOW)
    await service.run(NOW + timedelta(days=1))
    assert service.latest().name == "2026-09-21"


async def test_without_any_backup_there_is_nothing_to_find(db, private):
    settings = BackupSettings(directory=private / "없는폴더")
    assert BackupService(db, private, settings).latest() is None


# --- 실패 ---


async def test_a_failed_backup_is_told_about(db, private, monkeypatch):
    settings = BackupSettings(directory=private / "backups")
    service = BackupService(db, private, settings)

    def boom(*args, **kwargs):
        raise OSError("디스크가 가득 찼습니다")

    monkeypatch.setattr("app.storage.backup.shutil.copy2", boom)
    events = await service.run(NOW)

    assert len(events) == 1
    event = events[0]
    assert event.kind == EventKind.COLLECTOR_FAILED
    assert event.title == "백업이 되지 않았습니다"
    assert event.meta["reason"] == "OSError"
    # 같은 원인이 이어지면 게이트가 한 번만 알린다
    assert event.ref_id == "backup:failed:OSError"


async def test_a_failure_message_does_not_leak_what_went_wrong_in_detail(db, private, monkeypatch):
    """예외 메시지에는 경로나 비밀값이 섞일 수 있어 종류만 남긴다."""
    settings = BackupSettings(directory=private / "backups")

    def boom(*args, **kwargs):
        raise OSError("/home/ubuntu/private/.env 를 읽을 수 없습니다")

    monkeypatch.setattr("app.storage.backup.shutil.copy2", boom)
    events = await BackupService(db, private, settings).run(NOW)
    assert ".env" not in events[0].body
