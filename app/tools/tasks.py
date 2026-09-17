"""리마인더·예약 작업 도구."""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from app.core.clock import format_kst, utc_now
from app.core.interfaces import ToolResult
from app.scheduler.tasks import TaskError, TaskService
from app.storage.tasks import ScheduledTask
from app.tools.common import TIME_FORMAT_HINT, SimpleTool, ToolInputError, optional_str, parse_datetime, require_str, spec

_SCHEDULE_PROPS = {
    "run_at": {"type": "string", "description": f"1회 실행 시각. {TIME_FORMAT_HINT}"},
    "cron": {
        "type": "string",
        "description": "반복 규칙. cron 5항목(분 시 일 월 요일), Asia/Seoul 기준. 예: 매주 월요일 9시 '0 9 * * mon'",
    },
}
_STATUS = {"active": "활성", "paused": "일시정지"}


def task_tools(service: TaskService, clock: Callable[[], datetime] = utc_now) -> list:
    async def create(kind: str, content_key: str, args: Mapping[str, Any]) -> ToolResult:
        content = require_str(args, content_key, max_len=500)
        run_at_text, cron = optional_str(args, "run_at"), optional_str(args, "cron")
        try:
            task = await service.create(
                kind,
                content,
                clock(),
                run_at=parse_datetime(run_at_text) if run_at_text else None,
                cron=cron or None,
            )
        except TaskError as exc:
            raise ToolInputError(str(exc)) from exc
        return ToolResult(f"등록했습니다: {describe(task)}")

    async def reminder(args: Mapping[str, Any]) -> ToolResult:
        return await create("reminder", "message", args)

    async def agent_task(args: Mapping[str, Any]) -> ToolResult:
        return await create("agent", "instruction", args)

    async def list_tasks(args: Mapping[str, Any]) -> ToolResult:
        tasks = await service.list()
        if not tasks:
            return ToolResult("등록된 리마인더·예약 작업이 없습니다.")
        return ToolResult("\n".join(describe(task) for task in tasks))

    def change(action: Callable[[str], Any], done: str, failed: str):
        async def handler(args: Mapping[str, Any]) -> ToolResult:
            task_id = require_str(args, "task_id", max_len=20)
            if not await action(task_id):
                raise ToolInputError(f"{task_id}: {failed}")
            return ToolResult(f"{task_id} {done}")
        return handler

    def describe(task: ScheduledTask) -> str:
        kind = "리마인더" if task.kind == "reminder" else "예약 작업"
        when = f"반복 {task.cron}" if task.cron else f"{format_kst(task.run_at)}"
        next_run = service.next_run(task.id)
        upcoming = f", 다음 실행 {format_kst(next_run)}" if next_run and task.status == "active" else ""
        return f"[{task.id}] {kind} · {task.content} ({when}{upcoming}, {_STATUS.get(task.status, task.status)})"

    task_id_prop = {"task_id": {"type": "string", "description": "작업 ID (예: t-1a2b3c)"}}
    return [
        SimpleTool(
            spec(
                "create_reminder",
                "정해진 시각에 사용자님께 알림 메시지를 보내도록 예약한다. run_at(1회)과 cron(반복) 중 하나만 지정한다. "
                "사용자님이 직접 요청한 알림이므로 밤 시간에도 그대로 발송된다.",
                {"message": {"type": "string", "description": "보낼 알림 문장"}, **_SCHEDULE_PROPS},
                ["message"],
            ),
            reminder,
        ),
        SimpleTool(
            spec(
                "create_scheduled_task",
                "정해진 시각에 비서가 처리할 작업을 예약한다 (예: 매일 아침 할 일을 보고 우선순위 제안). "
                "실행 시 비서가 instruction을 수행하고 결과를 사용자님께 보낸다. 단순 알림은 create_reminder를 쓴다. "
                "반복은 하루 4회 이하만 가능하다.",
                {"instruction": {"type": "string", "description": "실행 시 비서가 할 일"}, **_SCHEDULE_PROPS},
                ["instruction"],
            ),
            agent_task,
        ),
        SimpleTool(
            spec("list_scheduled_tasks", "활성·일시정지 상태의 리마인더와 예약 작업을 조회한다.", {}, []),
            list_tasks,
        ),
        SimpleTool(
            spec("pause_scheduled_task", "리마인더·예약 작업을 일시정지한다.", task_id_prop, ["task_id"]),
            change(service.pause, "일시정지했습니다.", "활성 상태인 작업이 아닙니다."),
        ),
        SimpleTool(
            spec("resume_scheduled_task", "일시정지한 리마인더·예약 작업을 다시 시작한다.", task_id_prop, ["task_id"]),
            change(service.resume, "다시 시작했습니다.", "일시정지 상태인 작업이 아닙니다."),
        ),
        SimpleTool(
            spec("cancel_scheduled_task", "리마인더·예약 작업을 취소한다. 실행 기록은 남는다.", task_id_prop, ["task_id"]),
            change(service.cancel, "취소했습니다.", "활성·일시정지 상태인 작업이 아닙니다."),
        ),
    ]
