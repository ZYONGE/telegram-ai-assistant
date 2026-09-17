"""리마인더·예약 작업 실행 (NanoClaw 참고, docs/adr/0002).

- scheduled_tasks 표가 원본이고, APScheduler는 메모리에서 실행 시각만 계산한다.
  시작할 때 활성 작업을 모두 다시 등록하므로 재시작 후에도 예약이 유지된다.
- 발송은 알림 게이트를 거친다. 사용자가 요청한 작업이라 조용한 시간·일일 상한의 예외가 된다.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.core.clock import KST, require_aware, to_kst, utc_now
from app.core.events import Event, EventKind, EventSource
from app.scheduler.dispatcher import Dispatcher
from app.storage.tasks import ACTIVE, CANCELLED, COMPLETED, PAUSED, ScheduledTask, TaskRepository

logger = logging.getLogger(__name__)

KINDS = ("reminder", "agent")
# 24시간 안의 최대 실행 횟수. 모델을 부르는 작업은 비용 때문에 더 엄격하다.
MAX_DAILY_FIRES = {"reminder": 48, "agent": 4}
# 연속으로 이만큼 실패하면 반복 작업을 일시정지한다
AUTO_PAUSE_AFTER = 8
# 1회성 작업이 실패하면 이 간격으로 다시 시도한다
ONE_SHOT_RETRIES = 3
ONE_SHOT_RETRY_DELAY = timedelta(minutes=5)

AgentRunner = Callable[[str, datetime], Awaitable[str]]


class TaskError(ValueError):
    pass


def build_cron(expr: str) -> CronTrigger:
    if len(expr.split()) != 5:
        raise TaskError(f"cron 식은 '분 시 일 월 요일' 5개 항목이어야 합니다: {expr!r}")
    try:
        return CronTrigger.from_crontab(expr, timezone=KST)
    except ValueError as exc:
        raise TaskError(f"cron 식이 잘못되었습니다: {expr!r} ({exc})") from exc


def count_fires(trigger: CronTrigger, now: datetime, limit: int) -> int:
    """앞으로 24시간 동안의 실행 횟수. limit을 넘으면 그 즉시 멈춘다."""
    horizon = now + timedelta(hours=24)
    fires, previous, cursor = 0, None, now
    while fires <= limit:
        next_time = trigger.get_next_fire_time(previous, cursor)
        if next_time is None or next_time > horizon:
            break
        fires += 1
        previous, cursor = next_time, next_time + timedelta(seconds=1)
    return fires


class TaskService:
    def __init__(
        self,
        repo: TaskRepository,
        scheduler: BaseScheduler,
        dispatcher: Dispatcher,
        agent_runner: AgentRunner | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repo = repo
        self._scheduler = scheduler
        self._dispatcher = dispatcher
        self._agent_runner = agent_runner
        self._clock = clock

    def set_agent_runner(self, runner: AgentRunner) -> None:
        self._agent_runner = runner

    async def start(self) -> int:
        tasks = await self._repo.list((ACTIVE,))
        for task in tasks:
            self._schedule(task)
        return len(tasks)

    async def create(
        self, kind: str, content: str, now: datetime, *, run_at: datetime | None = None, cron: str | None = None
    ) -> ScheduledTask:
        if kind not in KINDS:
            raise TaskError(f"작업 종류는 {KINDS} 중 하나여야 합니다.")
        if (run_at is None) == (cron is None):
            raise TaskError("실행 시각(run_at)과 반복 규칙(cron) 중 하나만 지정해 주세요.")
        if run_at is not None:
            require_aware(run_at, "run_at")
            if run_at <= now:
                raise TaskError("이미 지난 시각입니다. 현재 시각 이후로 지정해 주세요.")
        if cron is not None:
            trigger = build_cron(cron)
            limit = MAX_DAILY_FIRES[kind]
            if count_fires(trigger, now, limit) > limit:
                raise TaskError(f"너무 자주 실행되는 작업입니다. 24시간에 {limit}회 이하로 정해 주세요.")
        task = await self._repo.create(kind, content, now, run_at=run_at, cron=cron)
        self._schedule(task)
        return task

    async def list(self) -> list[ScheduledTask]:
        return await self._repo.list((ACTIVE, PAUSED))

    async def get(self, task_id: str) -> ScheduledTask | None:
        return await self._repo.get(task_id)

    def next_run(self, task_id: str) -> datetime | None:
        job = self._scheduler.get_job(task_id)
        return getattr(job, "next_run_time", None) if job else None

    async def pause(self, task_id: str) -> bool:
        changed = await self._repo.set_status(task_id, PAUSED, expected=(ACTIVE,))
        if changed:
            self._unschedule(task_id)
        return changed

    async def resume(self, task_id: str) -> bool:
        changed = await self._repo.set_status(task_id, ACTIVE, expected=(PAUSED,))
        if changed:
            task = await self._repo.get(task_id)
            assert task is not None
            self._schedule(task)
        return changed

    async def cancel(self, task_id: str) -> bool:
        changed = await self._repo.set_status(task_id, CANCELLED, expected=(ACTIVE, PAUSED))
        if changed:
            self._unschedule(task_id)
        return changed

    async def fire(self, task_id: str) -> None:
        now = self._clock()
        task = await self._repo.get(task_id)
        if task is None or task.status != ACTIVE:
            return
        try:
            event = await self._build_event(task, now)
            await self._dispatcher.publish(event, now)
        except Exception as exc:
            logger.exception("예약 작업 실행 실패: %s", task_id)
            failures = await self._repo.record_run(task_id, now, False, type(exc).__name__)
            await self._handle_failure(task, failures, now)
            return
        await self._repo.record_run(task_id, now, True, "발송")
        if not task.recurring:
            await self._repo.set_status(task_id, COMPLETED, expected=(ACTIVE,))

    async def _build_event(self, task: ScheduledTask, now: datetime) -> Event:
        # 같은 회차를 다시 시도해도 알림이 두 번 가지 않게 회차별로 고정된 ref_id를 쓴다
        occurrence = f"{to_kst(now):%Y%m%d%H%M}" if task.recurring else "once"
        if task.kind == "reminder":
            title, body = task.content, ""
        else:
            if self._agent_runner is None:
                raise RuntimeError("예약 작업 실행기가 설정되지 않았습니다")
            title, body = "예약 작업 결과", await self._agent_runner(task.content, now)
        return Event(
            source=EventSource.SCHEDULER,
            kind=EventKind.REMINDER,
            title=title,
            body=body,
            user_requested=True,
            ref_id=f"task:{task.id}:{occurrence}",
        )

    async def _handle_failure(self, task: ScheduledTask, failures: int, now: datetime) -> None:
        if not task.recurring:
            if failures < ONE_SHOT_RETRIES:
                self._scheduler.add_job(
                    self.fire, DateTrigger(run_date=now + ONE_SHOT_RETRY_DELAY), args=[task.id],
                    id=task.id, replace_existing=True, misfire_grace_time=None,
                )
                return
            await self._repo.set_status(task.id, COMPLETED, expected=(ACTIVE,))
        elif failures < AUTO_PAUSE_AFTER:
            return
        else:
            await self.pause(task.id)
        await self._dispatcher.publish(
            Event(
                source=EventSource.SCHEDULER,
                kind=EventKind.NOTICE,
                title="예약 작업을 실행하지 못했습니다",
                body=f"{task.content[:60]} ({task.id}) — {failures}회 연속 실패로 중단했습니다.",
                urgent=True,
                ref_id=f"task:{task.id}:stopped:{to_kst(now):%Y%m%d%H%M}",
            ),
            now,
        )

    def _schedule(self, task: ScheduledTask) -> None:
        if task.cron is not None:
            trigger, grace = build_cron(task.cron), 3600
        else:
            # 꺼져 있는 동안 지난 1회성 알림은 켜지는 즉시 보낸다
            trigger, grace = DateTrigger(run_date=task.run_at), None
        self._scheduler.add_job(
            self.fire, trigger, args=[task.id], id=task.id, replace_existing=True,
            misfire_grace_time=grace, coalesce=True, max_instances=1,
        )

    def _unschedule(self, task_id: str) -> None:
        if self._scheduler.get_job(task_id) is not None:
            self._scheduler.remove_job(task_id)
