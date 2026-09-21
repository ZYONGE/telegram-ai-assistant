"""마감 리마인더: 정해 둔 시점마다 한 번, 끝낸 일은 조용히, 조용한 시간에는 넘긴다."""

from datetime import timedelta

import pytest

from app.core.config import ConfigError, NotificationSettings, parse_durations
from app.core.events import Event, EventKind
from app.scheduler.deadlines import DeadlineReminder, remaining, stage
from app.scheduler.ingest import Ingestor
from app.storage.todos import Todo
from tests.conftest import kst

DUE = kst(9, 25, 23, 59)
OFFSETS = NotificationSettings().deadline_reminders


def todo(created=kst(9, 1, 9), due=DUE) -> Todo:
    return Todo(1, "3장 과제", "", due, "open", "chat", None, created, created)


@pytest.mark.parametrize(
    ("left", "expected"),
    [
        (timedelta(days=8), None),
        (timedelta(days=6), timedelta(days=7)),
        (timedelta(days=2), timedelta(days=4)),
        (timedelta(hours=20), timedelta(days=1)),
        (timedelta(hours=5), timedelta(hours=12)),
        (timedelta(minutes=50), timedelta(hours=1)),
        (timedelta(minutes=-1), None),  # 이미 지났다
    ],
)
def test_the_nearest_passed_point_is_the_one_to_send(left, expected):
    assert stage(todo(), DUE - left, OFFSETS) == expected


def test_points_already_passed_when_the_todo_was_made_are_skipped():
    """방금 '이틀 뒤까지'라고 적은 일을 곧바로 '4일 남았다'며 재촉하지 않는다."""
    fresh = todo(created=DUE - timedelta(days=2))
    assert stage(fresh, DUE - timedelta(days=2) + timedelta(minutes=5), OFFSETS) is None
    assert stage(fresh, DUE - timedelta(hours=20), OFFSETS) == timedelta(days=1)


def test_remaining_time_reads_naturally():
    assert remaining(timedelta(days=4, hours=5)) == "4일"
    assert remaining(timedelta(days=1, hours=3)) == "1일 3시간"
    assert remaining(timedelta(hours=2, minutes=40)) == "2시간 40분"
    assert remaining(timedelta(hours=11, minutes=40)) == "11시간"
    assert remaining(timedelta(minutes=55)) == "55분"


def test_reminder_points_come_from_the_settings():
    assert parse_durations(["7d", "12h", "30m"]) == (
        timedelta(days=7),
        timedelta(hours=12),
        timedelta(minutes=30),
    )
    with pytest.raises(ConfigError):
        parse_durations(["사흘"])


async def test_each_point_is_sent_once(todos, dispatcher, gate, notifier):
    await todos.add("3장 과제", kst(9, 1, 9), due_at=DUE)
    reminder = DeadlineReminder(todos, dispatcher, gate, OFFSETS)

    now = DUE - timedelta(hours=2, minutes=30)
    assert await reminder.run(now) == 1
    assert await reminder.run(now + timedelta(minutes=5)) == 0
    assert "3장 과제 마감까지 2시간 30분 남았습니다." in notifier.sent[0].text
    # 다음 시점(1시간 전)이 오면 또 한 번
    assert await reminder.run(DUE - timedelta(minutes=62)) == 1


async def test_the_last_hour_of_a_2359_deadline_is_not_lost_to_quiet_hours(todos, dispatcher, gate):
    """23:59 마감의 1시간 전은 22:59, 조용한 시간 1분 전이다. 5분마다 보면 늦게 알아채 놓치기 쉽다."""
    await todos.add("3장 과제", kst(9, 1, 9), due_at=DUE)
    reminder = DeadlineReminder(todos, dispatcher, gate, OFFSETS)
    await reminder.run(DUE - timedelta(hours=2, minutes=30))
    # 확인 간격만큼 일찍 알아챈다 (22:55)
    assert await reminder.run(kst(9, 25, 22, 55)) == 1


async def test_reminders_skip_the_daily_limit(todos, dispatcher, gate, notifier, settings):
    """사용자가 정한 알림이라 하루 상한(여기서는 2건)에 걸리지 않는다."""
    for index in range(settings.daily_limit + 2):
        await todos.add(f"과제 {index}", kst(9, 1, 9), due_at=DUE)
    sent = await DeadlineReminder(todos, dispatcher, gate, OFFSETS).run(DUE - timedelta(hours=2))
    assert sent == settings.daily_limit + 2


async def test_finished_todos_stay_quiet(todos, dispatcher, gate, notifier):
    item = await todos.add("3장 과제", kst(9, 1, 9), due_at=DUE)
    await todos.set_status(item.id, "done", kst(9, 20, 9))
    assert await DeadlineReminder(todos, dispatcher, gate, OFFSETS).run(DUE - timedelta(hours=2)) == 0


async def test_quiet_hours_are_skipped_not_held(todos, dispatcher, gate, notifier, log):
    """보류했다 아침에 보내면 이미 지난 '1시간 전' 알림이 간다. 넘기고 아침에 남은 시간으로 알린다."""
    due = kst(9, 26, 8, 0)
    await todos.add("아침 제출", kst(9, 1, 9), due_at=due)
    reminder = DeadlineReminder(todos, dispatcher, gate, OFFSETS)

    assert await reminder.run(kst(9, 26, 5, 30)) == 0  # 조용한 시간, 3시간 전 시점
    assert await log.pending_delivery(kst(9, 26, 6, 30)) == []
    assert await reminder.run(kst(9, 26, 6, 35)) == 1
    assert "1시간 25분" in notifier.sent[0].text


async def test_a_changed_deadline_moves_the_todo(todos, dispatcher):
    ref = "eclass:과제:KJ1:111"
    await todos.add_from_event(
        Event(source="eclass", kind=EventKind.DEADLINE, title="3장 과제", ref_id=ref, due_at=DUE), kst(9, 1, 9)
    )
    later = DUE + timedelta(days=2)
    changed = Event(
        source="eclass",
        kind=EventKind.DEADLINE_CHANGED,
        title="마감이 바뀌었습니다",
        ref_id=f"{ref}:due:{later.isoformat()}",
        due_at=later,
        meta={"todo_ref": ref},
    )
    await Ingestor(todos, dispatcher).ingest([changed], kst(9, 20, 9))
    assert (await todos.get_by_ref(ref)).due_at == later
