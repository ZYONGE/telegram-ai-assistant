from datetime import timedelta

import pytest

from app.core.events import EventKind
from app.core.interfaces import GateAction
from app.scheduler.tasks import AUTO_PAUSE_AFTER, ONE_SHOT_RETRIES, TaskError, TaskService
from app.storage.tasks import ACTIVE, COMPLETED, PAUSED
from tests.conftest import kst


async def test_one_shot_reminder_fires_even_in_quiet_hours(task_service, task_repo, notifier, log, clock):
    task = await task_service.create("reminder", "약 먹기", clock(), run_at=kst(9, 17, 23, 30))
    assert task_service.next_run(task.id) == kst(9, 17, 23, 30)

    clock.now = kst(9, 17, 23, 30)
    await task_service.fire(task.id)

    assert [m.text for m in notifier.sent] == ["약 먹기"]
    record = await log.get(f"task:{task.id}:once")
    assert record.event.user_requested and record.event.kind == EventKind.REMINDER
    assert (await task_repo.get(task.id)).status == COMPLETED

    await task_service.fire(task.id)
    assert len(notifier.sent) == 1


async def test_recurring_reminder_stays_active_and_dedups_same_minute(task_service, task_repo, notifier, clock):
    task = await task_service.create("reminder", "물 마시기", clock(), cron="0 15 * * *")
    clock.now = kst(9, 17, 15)
    await task_service.fire(task.id)
    await task_service.fire(task.id)
    clock.now = kst(9, 18, 15)
    await task_service.fire(task.id)

    assert [m.text for m in notifier.sent] == ["물 마시기", "물 마시기"]
    assert (await task_repo.get(task.id)).status == ACTIVE
    assert len(await task_repo.runs(task.id)) == 3


async def test_paused_task_does_not_fire(task_service, notifier, clock):
    task = await task_service.create("reminder", "산책", clock(), cron="0 15 * * *")
    assert await task_service.pause(task.id)
    assert task_service.next_run(task.id) is None
    await task_service.fire(task.id)
    assert notifier.sent == []

    assert await task_service.resume(task.id)
    assert task_service.next_run(task.id) is not None


async def test_start_restores_only_active_tasks(task_repo, scheduler, dispatcher, clock):
    first = TaskService(task_repo, scheduler, dispatcher, clock=clock)
    active = await first.create("reminder", "A", clock(), run_at=kst(9, 18, 9))
    paused = await first.create("reminder", "B", clock(), run_at=kst(9, 18, 10))
    await first.pause(paused.id)
    scheduler.remove_all_jobs()

    restarted = TaskService(task_repo, scheduler, dispatcher, clock=clock)
    assert await restarted.start() == 1
    assert restarted.next_run(active.id) == kst(9, 18, 9)
    assert restarted.next_run(paused.id) is None


async def test_agent_task_sends_runner_result(task_service, notifier, clock):
    async def runner(instruction, now):
        assert instruction == "할 일 우선순위 정리"
        return "오늘은 보고서부터 하시면 좋겠습니다."

    task_service.set_agent_runner(runner)
    task = await task_service.create("agent", "할 일 우선순위 정리", clock(), run_at=kst(9, 17, 18))
    clock.now = kst(9, 17, 18)
    await task_service.fire(task.id)
    assert notifier.sent[0].text == "예약 작업 결과\n오늘은 보고서부터 하시면 좋겠습니다."


async def test_failing_one_shot_retries_then_stops_with_notice(task_service, task_repo, notifier, scheduler, clock):
    async def broken(instruction, now):
        raise RuntimeError("api down")

    task_service.set_agent_runner(broken)
    task = await task_service.create("agent", "정리", clock(), run_at=kst(9, 17, 15))

    for attempt in range(1, ONE_SHOT_RETRIES):
        clock.now = kst(9, 17, 15) + timedelta(minutes=5 * attempt)
        await task_service.fire(task.id)
        job = scheduler.get_job(task.id)
        assert job is not None and job.next_run_time == clock.now + timedelta(minutes=5)
        assert notifier.sent == []

    clock.now += timedelta(minutes=5)
    await task_service.fire(task.id)
    assert (await task_repo.get(task.id)).status == COMPLETED
    assert notifier.sent[0].text.startswith("예약 작업을 실행하지 못했습니다")


async def test_recurring_task_auto_pauses_after_repeated_failures(task_service, task_repo, notifier, clock):
    async def broken(instruction, now):
        raise RuntimeError("api down")

    task_service.set_agent_runner(broken)
    task = await task_service.create("agent", "점검", clock(), cron="0 15 * * *")
    for day in range(AUTO_PAUSE_AFTER):
        clock.now = kst(9, 17, 15) + timedelta(days=day)
        await task_service.fire(task.id)

    assert (await task_repo.get(task.id)).status == PAUSED
    assert task_service.next_run(task.id) is None
    assert len(notifier.sent) == 1 and "8회 연속 실패" in notifier.sent[0].text


async def test_invalid_kind_is_rejected(task_service, clock):
    with pytest.raises(TaskError):
        await task_service.create("email", "x", clock(), run_at=kst(9, 18, 9))


async def test_notice_for_failure_goes_through_gate(task_service, log, clock):
    """실패 안내도 게이트를 거친다: 조용한 시간이면 보류된다."""

    async def broken(instruction, now):
        raise RuntimeError("api down")

    task_service.set_agent_runner(broken)
    task = await task_service.create("agent", "정리", clock(), run_at=kst(9, 17, 23, 30))
    clock.now = kst(9, 17, 23, 30)
    for _ in range(ONE_SHOT_RETRIES):
        await task_service.fire(task.id)
        clock.now += timedelta(minutes=5)
    held = [r for r in await log.pending_delivery(kst(9, 18, 7)) if "stopped" in r.event.ref_id]
    assert len(held) == 1 and held[0].action is GateAction.HOLD
