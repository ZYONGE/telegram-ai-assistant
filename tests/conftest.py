import copy
from datetime import datetime

import pytest
from anthropic.types import Message, TextBlock, ThinkingBlock, ToolUseBlock, Usage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.clock import KST
from app.core.config import NotificationSettings
from app.core.events import Event, EventKind
from app.core.interfaces import OutgoingMessage
from app.scheduler.dispatcher import Dispatcher
from app.scheduler.gate import RuleBasedGate
from app.scheduler.tasks import TaskService
from app.storage.conversation import ConversationStore, PendingActionStore
from app.storage.db import Database
from app.storage.notifications import NotificationLog
from app.storage.tasks import TaskRepository
from app.storage.todos import TodoRepository
from app.tools.registry import ToolRegistry


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


class Clock:
    """테스트에서 현재 시각을 고정하고 옮길 수 있는 시계."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return Clock(kst(9, 17, 14))


@pytest.fixture
def conversation(db):
    return ConversationStore(db)


@pytest.fixture
def pending(db):
    return PendingActionStore(db)


@pytest.fixture
def registry(pending):
    return ToolRegistry(pending)


@pytest.fixture
def task_repo(db):
    return TaskRepository(db)


@pytest.fixture
async def scheduler():
    sched = AsyncIOScheduler(timezone=KST)
    sched.start(paused=True)
    yield sched
    sched.shutdown(wait=False)


@pytest.fixture
def task_service(task_repo, scheduler, dispatcher, clock):
    return TaskService(task_repo, scheduler, dispatcher, clock=clock)


# --- Anthropic API 가짜 클라이언트 ---


def text(value: str) -> TextBlock:
    return TextBlock(type="text", text=value)


def tool_use(tool_id: str, name: str, args: dict) -> ToolUseBlock:
    return ToolUseBlock(type="tool_use", id=tool_id, name=name, input=args)


def thinking(signature: str = "sig") -> ThinkingBlock:
    return ThinkingBlock(type="thinking", thinking="", signature=signature)


def response(*blocks, stop: str = "end_turn") -> Message:
    return Message.model_construct(
        id="msg_test",
        type="message",
        role="assistant",
        model="test-model",
        content=list(blocks),
        stop_reason=stop,
        stop_sequence=None,
        usage=Usage(input_tokens=1, output_tokens=1),
    )


class FakeMessages:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeAnthropic:
    def __init__(self, *responses) -> None:
        self.messages = FakeMessages(responses)
