from datetime import datetime

import pytest

from app.core.clock import KST
from app.core.config import NotificationSettings
from app.core.events import Event, EventKind
from app.core.interfaces import OutgoingMessage
from app.scheduler.dispatcher import Dispatcher
from app.scheduler.gate import RuleBasedGate
from app.storage.db import Database
from app.storage.notifications import NotificationLog
from app.storage.todos import TodoRepository


def kst(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=KST)


def make_event(ref_id: str = "e1", **overrides) -> Event:
    fields = dict(source="eclass", kind=EventKind.NOTICE, title="휴강 안내", ref_id=ref_id)
    return Event(**(fields | overrides))


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []
        self.fail = False

    async def send(self, message: OutgoingMessage) -> None:
        if self.fail:
            raise ConnectionError("텔레그램 연결 실패")
        self.sent.append(message)


@pytest.fixture
async def db(tmp_path):
    database = await Database.open(tmp_path / "test.db")
    yield database
    await database.close()


@pytest.fixture
def log(db):
    return NotificationLog(db)


@pytest.fixture
def todos(db):
    return TodoRepository(db)


@pytest.fixture
def settings():
    return NotificationSettings(daily_limit=2)


@pytest.fixture
def gate(log, settings):
    return RuleBasedGate(log, settings)


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture
def dispatcher(gate, log, notifier):
    return Dispatcher(gate, log, notifier)
