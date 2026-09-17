from datetime import datetime
from types import SimpleNamespace

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from google.genai import errors as genai_errors
from google.genai import types

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


# --- Gemini API 가짜 클라이언트 (실제 GeminiModel 어댑터와 함께 쓴다) ---


def text_part(value: str, signature: bytes | None = None) -> types.Part:
    return types.Part(text=value, thought_signature=signature)


def call_part(call_id: str, name: str, args: dict, signature: bytes | None = None) -> types.Part:
    return types.Part(function_call=types.FunctionCall(id=call_id, name=name, args=args), thought_signature=signature)


def gemini_response(*parts: types.Part, finish: str = "STOP") -> types.GenerateContentResponse:
    content = types.Content(role="model", parts=list(parts)) if parts else None
    return types.GenerateContentResponse(candidates=[types.Candidate(content=content, finish_reason=finish)])


def blocked_prompt_response() -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[], prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason="SAFETY")
    )


def server_error() -> genai_errors.ServerError:
    return genai_errors.ServerError(503, {"error": {"code": 503, "message": "unavailable", "status": "UNAVAILABLE"}})


class FakeAsyncModels:
    def __init__(self, responses, missing) -> None:
        self.responses = list(responses)
        self.missing = set(missing)
        self.calls: list[dict] = []
        self.checked: list[str] = []

    async def generate_content(self, *, model, contents, config):
        self.calls.append(
            {
                "model": model,
                "contents": [c.model_dump(mode="json", exclude_none=True) for c in contents],
                "config": config,
            }
        )
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def get(self, *, model):
        self.checked.append(model)
        if model in self.missing:
            raise genai_errors.ClientError(404, {"error": {"code": 404, "message": "not found", "status": "NOT_FOUND"}})
        return types.Model(name=f"models/{model}")


class FakeGenAI:
    def __init__(self, *responses, missing=()) -> None:
        self.models = FakeAsyncModels(responses, missing)
        self.closed = False
        self.aio = SimpleNamespace(models=self.models, aclose=self._aclose)

    async def _aclose(self) -> None:
        self.closed = True
