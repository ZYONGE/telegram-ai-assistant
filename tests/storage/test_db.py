from datetime import UTC, datetime

import pytest

from app.storage.db import MIGRATIONS, Database, from_db_time, to_db_time
from tests.conftest import kst, make_event


async def test_migrations_apply_once(tmp_path):
    path = tmp_path / "nested" / "assistant.db"
    first = await Database.open(path)
    assert await first.schema_version() == len(MIGRATIONS)
    await first.close()

    second = await Database.open(path)
    assert await second.schema_version() == len(MIGRATIONS)
    await second.close()


def test_db_time_is_utc_fixed_width():
    assert to_db_time(kst(9, 17, 9)) == "2026-09-17T00:00:00.000000+00:00"
    assert from_db_time(to_db_time(kst(9, 17, 9))) == datetime(2026, 9, 17, 0, 0, tzinfo=UTC)
    assert to_db_time(None) is None


def test_db_time_rejects_naive():
    with pytest.raises(ValueError):
        to_db_time(datetime(2026, 9, 17))


async def test_open_todos_are_ordered_by_due_date(todos):
    now = kst(9, 17, 14)
    await todos.add_from_event(make_event("no-due", title="마감 없음"), now)
    await todos.add_from_event(make_event("late", title="나중", due_at=kst(9, 25, 23, 59)), now)
    await todos.add_from_event(make_event("soon", title="먼저", due_at=kst(9, 19, 23, 59)), now)

    assert [t.title for t in await todos.list_open()] == ["먼저", "나중", "마감 없음"]


async def test_add_from_event_reports_whether_created(todos):
    now = kst(9, 17, 14)
    first, created = await todos.add_from_event(make_event("x"), now)
    again, created_again = await todos.add_from_event(make_event("x", title="다른 제목"), now)

    assert created is True and created_again is False
    assert again.id == first.id and again.title == "휴강 안내"


async def test_notification_record_round_trips_event(log):
    from app.core.interfaces import GateAction, GateDecision

    event = make_event("r1", urgent=True, user_requested=True, due_at=kst(9, 18, 9), meta={"course": "자료구조"})
    await log.save_decision(event, GateDecision(GateAction.BATCH, "묶음"), kst(9, 17, 14))
    record = await log.get("r1")
    assert record.event == event
    assert record.action is GateAction.BATCH and record.sent_at is None


async def test_drop_decision_is_not_saved(log):
    from app.core.interfaces import GateAction, GateDecision

    with pytest.raises(ValueError):
        await log.save_decision(make_event(), GateDecision(GateAction.DROP, "중복"), kst(9, 17, 14))
