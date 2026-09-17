from datetime import timedelta

import pytest

from app.agent.memory import MarkdownMemoryStore
from app.core.clock import KST
from app.core.interfaces import Confirmation, ToolResult, ToolSpec
from app.tools.common import ToolInputError, parse_datetime, parse_due
from app.tools.memory import memory_tools
from app.tools.tasks import task_tools
from app.tools.todos import todo_tools
from tests.conftest import kst


@pytest.fixture
def todo_registry(registry, todos, clock):
    registry.register(*todo_tools(todos, clock))
    return registry


def test_parse_datetime_defaults_to_kst():
    assert parse_datetime("2026-09-20T18:00") == kst(9, 20, 18)
    assert parse_datetime("2026-09-20T09:00+00:00") == kst(9, 20, 18)
    with pytest.raises(ToolInputError):
        parse_datetime("내일 저녁")


def test_parse_due_date_only_means_end_of_day():
    assert parse_due("2026-09-20") == kst(9, 20, 23, 59)
    assert parse_due("2026-09-20T10:30") == kst(9, 20, 10, 30)
    with pytest.raises(ToolInputError):
        parse_due("2026-13-01")


def test_definitions_are_sorted_and_filterable(todo_registry):
    names = [d["name"] for d in todo_registry.definitions()]
    assert names == sorted(names)
    assert "delete_todo" in names
    assert "delete_todo" not in [d["name"] for d in todo_registry.definitions(immediate_only=True)]
    schema = todo_registry.definitions()[0]["input_schema"]
    assert schema["type"] == "object" and schema["additionalProperties"] is False


def test_duplicate_tool_names_are_rejected(todo_registry, todos):
    with pytest.raises(ValueError):
        todo_registry.register(*todo_tools(todos))


async def test_todo_tools_round_trip(todo_registry, todos, clock):
    now = clock()
    added = await todo_registry.call("add_todo", {"title": "알고리즘 과제", "due": "2026-09-20"}, now)
    assert added.pending is None and not added.result.is_error
    assert "#1 알고리즘 과제 (마감 9월 20일(일) 23:59)" in added.result.content

    await todo_registry.call("add_todo", {"title": "지난 일", "due": "2026-09-16"}, now)
    listed = await todo_registry.call("list_todos", {}, now)
    assert listed.result.content.splitlines() == [
        "#2 지난 일 (마감 9월 16일(수) 23:59) [마감 지남]",
        "#1 알고리즘 과제 (마감 9월 20일(일) 23:59)",
    ]

    updated = await todo_registry.call("update_todo", {"todo_id": 1, "due": "", "notes": "2장까지"}, now)
    assert updated.result.content == "수정했습니다: #1 알고리즘 과제 — 2장까지"

    done = await todo_registry.call("complete_todo", {"todo_id": 2}, now)
    assert "[완료]" in done.result.content
    assert [t.id for t in await todos.list_open()] == [1]


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("add_todo", {}),
        ("add_todo", {"title": "x", "due": "모레"}),
        ("update_todo", {"todo_id": 1}),
        ("update_todo", {"todo_id": "1", "title": "x"}),
        ("complete_todo", {"todo_id": 99}),
        ("delete_todo", {"todo_id": 99}),
        ("unknown_tool", {}),
    ],
)
async def test_invalid_tool_input_returns_error_result(todo_registry, clock, name, args):
    outcome = await todo_registry.call(name, args, clock())
    assert outcome.result.is_error
    assert outcome.pending is None


async def test_delete_requires_confirmation(todo_registry, todos, clock):
    now = clock()
    await todo_registry.call("add_todo", {"title": "지울 일"}, now)

    outcome = await todo_registry.call("delete_todo", {"todo_id": 1}, now)
    assert outcome.pending is not None
    assert "아직 실행되지 않았습니다" in outcome.result.content
    assert outcome.pending.summary == "할 일 삭제 — #1 지울 일"
    assert await todos.get(1) is not None

    action, result = await todo_registry.confirm(outcome.pending.id, now)
    assert result.content == "삭제했습니다: #1 지울 일"
    assert await todos.get(1) is None
    assert await todo_registry.confirm(outcome.pending.id, now) is None


async def test_cancelled_confirmation_does_not_run(todo_registry, todos, clock):
    now = clock()
    await todo_registry.call("add_todo", {"title": "남길 일"}, now)
    outcome = await todo_registry.call("delete_todo", {"todo_id": 1}, now)

    assert (await todo_registry.cancel(outcome.pending.id, now)).tool_name == "delete_todo"
    assert await todo_registry.confirm(outcome.pending.id, now) is None
    assert await todos.get(1) is not None


async def test_tool_crash_becomes_error_result(registry, clock):
    class Broken:
        spec = ToolSpec("broken", "항상 실패", {"type": "object", "properties": {}}, Confirmation.IMMEDIATE)

        async def run(self, args) -> ToolResult:
            raise RuntimeError("secret detail")

    registry.register(Broken())
    outcome = await registry.call("broken", {}, clock())
    assert outcome.result.is_error and "secret" not in outcome.result.content


async def test_memory_tools(registry, tmp_path, clock):
    store = MarkdownMemoryStore(tmp_path / "memory.md")
    registry.register(*memory_tools(store, clock))

    saved = await registry.call("remember", {"text": "아침 수업을 선호함"}, clock())
    assert saved.result.content.startswith("기록했습니다 [m-")
    item_id = (await store.read())[0].item_id

    refused = await registry.call("remember", {"text": "비밀번호는 1234"}, clock())
    assert refused.result.is_error and "비밀번호" in refused.result.content

    assert (await registry.call("forget", {"memory_id": item_id}, clock())).result.content == f"삭제했습니다: {item_id}"
    assert (await registry.call("forget", {"memory_id": item_id}, clock())).result.is_error


async def test_task_tools(registry, task_service, clock):
    registry.register(*task_tools(task_service, clock))
    now = clock()

    created = await registry.call("create_reminder", {"message": "과제 제출", "run_at": "2026-09-17T18:00"}, now)
    assert created.result.content.startswith("등록했습니다: [t-")
    assert "리마인더 · 과제 제출 (9월 17일(목) 18:00" in created.result.content
    task_id = created.result.content.split("[")[1].split("]")[0]

    repeating = await registry.call("create_reminder", {"message": "물 마시기", "cron": "0 9 * * *"}, now)
    assert "반복 0 9 * * *" in repeating.result.content

    listed = await registry.call("list_scheduled_tasks", {}, now)
    assert len(listed.result.content.splitlines()) == 2

    assert "일시정지했습니다" in (await registry.call("pause_scheduled_task", {"task_id": task_id}, now)).result.content
    assert (await registry.call("pause_scheduled_task", {"task_id": task_id}, now)).result.is_error
    assert "다시 시작했습니다" in (await registry.call("resume_scheduled_task", {"task_id": task_id}, now)).result.content
    assert "취소했습니다" in (await registry.call("cancel_scheduled_task", {"task_id": task_id}, now)).result.content


@pytest.mark.parametrize(
    "args",
    [
        {"message": "x"},
        {"message": "x", "run_at": "2026-09-17T18:00", "cron": "0 9 * * *"},
        {"message": "x", "run_at": "2026-09-17T13:00"},
        {"message": "x", "cron": "0 9 * *"},
        {"message": "x", "cron": "99 9 * * *"},
        {"message": "x", "cron": "*/10 * * * *"},
    ],
)
async def test_invalid_reminders_are_rejected(registry, task_service, clock, args):
    registry.register(*task_tools(task_service, clock))
    outcome = await registry.call("create_reminder", args, clock())
    assert outcome.result.is_error
    assert await task_service.list() == []


async def test_agent_tasks_have_stricter_frequency_limit(registry, task_service, clock):
    registry.register(*task_tools(task_service, clock))
    hourly = {"instruction": "할 일 점검", "cron": "0 * * * *"}
    assert (await registry.call("create_scheduled_task", hourly, clock())).result.is_error
    hourly_reminder = {"message": "스트레칭", "cron": "0 * * * *"}
    assert not (await registry.call("create_reminder", hourly_reminder, clock())).result.is_error
    four_a_day = {"instruction": "할 일 점검", "cron": "0 9,12,15,18 * * *"}
    assert not (await registry.call("create_scheduled_task", four_a_day, clock())).result.is_error
