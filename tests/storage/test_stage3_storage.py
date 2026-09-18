from datetime import timedelta

import pytest

from app.core.events import EventKind
from app.core.interfaces import GateAction, GateDecision
from app.storage.tasks import ACTIVE, CANCELLED, PAUSED
from tests.conftest import kst, make_event


async def test_todo_update_complete_delete(todos):
    now = kst(9, 17, 14)
    todo = await todos.add("보고서", now, due_at=kst(9, 20, 23, 59))

    updated = await todos.update(todo.id, now, title="보고서 초안", due_at=None)
    assert updated.title == "보고서 초안" and updated.due_at is None

    kept = await todos.update(todo.id, now, notes="3쪽")
    assert kept.title == "보고서 초안" and kept.notes == "3쪽"

    await todos.set_status(todo.id, "done", kst(9, 17, 15))
    assert await todos.list_open() == []
    assert [t.id for t in await todos.list_done_between(kst(9, 17, 0), kst(9, 18, 0))] == [todo.id]

    assert await todos.delete(todo.id) is True
    assert await todos.get(todo.id) is None
    assert await todos.update(todo.id, now, title="x") is None


async def test_task_status_transitions_and_run_log(task_repo):
    now = kst(9, 17, 14)
    task = await task_repo.create("reminder", "물 마시기", now, cron="0 9 * * *")
    assert task.id.startswith("t-") and task.recurring and task.status == ACTIVE

    assert await task_repo.set_status(task.id, ACTIVE, expected=(PAUSED,)) is False
    assert await task_repo.set_status(task.id, PAUSED, expected=(ACTIVE,)) is True
    assert [t.id for t in await task_repo.list()] == [task.id]
    assert await task_repo.set_status(task.id, CANCELLED, expected=(ACTIVE, PAUSED)) is True
    assert await task_repo.list() == []

    assert await task_repo.record_run(task.id, now, False, "RuntimeError") == 1
    assert await task_repo.record_run(task.id, now, False, "RuntimeError") == 2
    assert await task_repo.record_run(task.id, now, True, "발송") == 0
    assert [r.ok for r in await task_repo.runs(task.id)] == [True, False, False]


async def test_task_requires_exactly_one_schedule(task_repo):
    with pytest.raises(Exception):
        await task_repo.create("reminder", "x", kst(9, 17, 14))


async def test_conversation_append_archive_and_summary(conversation):
    now = kst(9, 17, 14)
    user_turn = {"role": "user", "parts": [{"text": "안녕"}]}
    await conversation.append("user", user_turn, now)
    await conversation.append("assistant", {"role": "model", "parts": [{"text": "네"}]}, now + timedelta(minutes=1))

    messages = await conversation.active_messages()
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[0].content == user_turn
    assert await conversation.last_activity() == now + timedelta(minutes=1)
    assert await conversation.count_active() == 2

    await conversation.archive_with_summary(messages[-1].id, "- 인사함", now)
    assert await conversation.active_messages() == []
    assert await conversation.summary() == "- 인사함"
    assert await conversation.last_activity() is None


async def test_migration_archives_conversations_from_previous_provider(tmp_path):
    import aiosqlite

    from app.storage.db import MIGRATIONS, Database

    # 예전 버전(마이그레이션 2까지)으로 만든 DB
    path = tmp_path / "old.db"
    old_conn = await aiosqlite.connect(path)
    for script in MIGRATIONS[:2]:
        await old_conn.executescript(script)
    await old_conn.execute("PRAGMA user_version = 2")
    await old_conn.execute(
        "INSERT INTO conversation_messages (role, content, created_at) VALUES ('user', '[]', '2026-09-17T00:00:00.000000+00:00')"
    )
    await old_conn.commit()
    await old_conn.close()

    reopened = await Database.open(path)
    assert await reopened.schema_version() == len(MIGRATIONS)
    from app.storage.conversation import ConversationStore

    assert await ConversationStore(reopened).active_messages() == []
    await reopened.close()


async def test_notes_are_consumed_once(conversation):
    now = kst(9, 17, 14)
    await conversation.add_note("삭제 확인됨", now)
    assert await conversation.consume_notes() == ["삭제 확인됨"]
    assert await conversation.consume_notes() == []


async def test_pending_action_resolves_once(pending):
    now = kst(9, 17, 14)
    action = await pending.create("delete_todo", {"todo_id": 1}, "할 일 삭제", now)
    assert (await pending.get(action.id)).args == {"todo_id": 1}
    assert await pending.resolve(action.id, "done", now) is True
    assert await pending.resolve(action.id, "cancelled", now) is False
    assert (await pending.get(action.id)).status == "done"


async def test_briefings_do_not_count_toward_daily_limit_and_batch_is_briefed_once(log):
    now = kst(9, 17, 7)
    briefing = make_event("b1", kind=EventKind.BRIEFING)
    await log.save_decision(briefing, GateDecision(GateAction.SEND_NOW, "브리핑"), now)
    await log.mark_sent("b1", now)
    assert await log.count_sent_since(kst(9, 17, 0)) == 0

    await log.save_decision(make_event("n1"), GateDecision(GateAction.BATCH, "묶음"), now)
    assert [r.event.ref_id for r in await log.unbriefed_batch()] == ["n1"]
    await log.mark_briefed(["n1"], now)
    assert await log.unbriefed_batch() == []
