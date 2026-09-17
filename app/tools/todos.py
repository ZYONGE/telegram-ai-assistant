"""할 일 도구."""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from app.core.clock import format_kst, utc_now
from app.core.interfaces import Confirmation, ToolResult
from app.storage.todos import Todo, TodoRepository
from app.tools.common import SimpleTool, ToolInputError, optional_str, parse_due, require_int, require_str, spec

_DUE_HINT = "마감. 'YYYY-MM-DD'(그날 23:59로 저장) 또는 'YYYY-MM-DDTHH:MM'. 시간대가 없으면 Asia/Seoul."


def format_todo(todo: Todo) -> str:
    due = f" (마감 {format_kst(todo.due_at)})" if todo.due_at else ""
    notes = f" — {todo.notes}" if todo.notes else ""
    done = " [완료]" if todo.status == "done" else ""
    return f"#{todo.id} {todo.title}{due}{notes}{done}"


def todo_tools(repo: TodoRepository, clock: Callable[[], datetime] = utc_now) -> list:
    async def add(args: Mapping[str, Any]) -> ToolResult:
        due = optional_str(args, "due")
        todo = await repo.add(
            require_str(args, "title", max_len=200),
            clock(),
            notes=optional_str(args, "notes") or "",
            due_at=parse_due(due) if due else None,
        )
        return ToolResult(f"추가했습니다: {format_todo(todo)}")

    async def list_open(args: Mapping[str, Any]) -> ToolResult:
        todos = await repo.list_open()
        if not todos:
            return ToolResult("열린 할 일이 없습니다.")
        now = clock()
        lines = [format_todo(t) + (" [마감 지남]" if t.due_at and t.due_at < now else "") for t in todos]
        return ToolResult("\n".join(lines))

    async def update(args: Mapping[str, Any]) -> ToolResult:
        todo_id = require_int(args, "todo_id")
        changes: dict[str, Any] = {}
        if (title := optional_str(args, "title")) is not None:
            changes["title"] = title or _raise("제목은 비울 수 없습니다.")
        if (notes := optional_str(args, "notes")) is not None:
            changes["notes"] = notes
        if (due := optional_str(args, "due")) is not None:
            changes["due_at"] = parse_due(due) if due else None
        if not changes:
            raise ToolInputError("바꿀 항목(title, notes, due) 중 하나 이상을 지정해 주세요.")
        todo = await repo.update(todo_id, clock(), **changes)
        if todo is None:
            raise ToolInputError(f"#{todo_id} 할 일이 없습니다.")
        return ToolResult(f"수정했습니다: {format_todo(todo)}")

    async def complete(args: Mapping[str, Any]) -> ToolResult:
        todo_id = require_int(args, "todo_id")
        todo = await repo.set_status(todo_id, "done", clock())
        if todo is None:
            raise ToolInputError(f"#{todo_id} 할 일이 없습니다.")
        return ToolResult(f"완료 처리했습니다: {format_todo(todo)}")

    todo_id_prop = {"todo_id": {"type": "integer", "description": "할 일 번호 (list_todos 결과의 # 뒤 숫자)"}}
    return [
        SimpleTool(
            spec(
                "add_todo",
                "할 일을 추가한다. 사용자가 대충 말한 내용도 짧은 제목과 메모로 정리해서 넣는다.",
                {
                    "title": {"type": "string", "description": "짧은 제목"},
                    "due": {"type": "string", "description": _DUE_HINT},
                    "notes": {"type": "string", "description": "세부 내용 (선택)"},
                },
                ["title"],
            ),
            add,
        ),
        SimpleTool(spec("list_todos", "완료되지 않은 할 일 목록을 마감 순으로 조회한다.", {}, []), list_open),
        SimpleTool(
            spec(
                "update_todo",
                "할 일의 제목·메모·마감을 수정한다. due에 빈 문자열을 주면 마감을 없앤다.",
                {
                    **todo_id_prop,
                    "title": {"type": "string"},
                    "notes": {"type": "string"},
                    "due": {"type": "string", "description": _DUE_HINT},
                },
                ["todo_id"],
            ),
            update,
        ),
        SimpleTool(spec("complete_todo", "할 일을 완료 처리한다.", todo_id_prop, ["todo_id"]), complete),
        DeleteTodoTool(repo, todo_id_prop),
    ]


class DeleteTodoTool:
    """할 일 삭제는 확인 버튼을 받은 뒤 실행한다."""

    def __init__(self, repo: TodoRepository, props: dict[str, Any]) -> None:
        self._repo = repo
        self.spec = spec(
            "delete_todo",
            "할 일을 삭제한다. 실행 전에 사용자에게 확인 버튼이 전송되고, 사용자가 확인해야 삭제된다. "
            "완료한 일은 삭제하지 말고 complete_todo를 쓴다.",
            props,
            ["todo_id"],
            confirmation=Confirmation.BUTTON,
        )

    async def describe(self, args: Mapping[str, Any]) -> str:
        todo = await self._get(args)
        return f"할 일 삭제 — {format_todo(todo)}"

    async def run(self, args: Mapping[str, Any]) -> ToolResult:
        todo = await self._get(args)
        await self._repo.delete(todo.id)
        return ToolResult(f"삭제했습니다: {format_todo(todo)}")

    async def _get(self, args: Mapping[str, Any]) -> Todo:
        todo_id = require_int(args, "todo_id")
        todo = await self._repo.get(todo_id)
        if todo is None:
            raise ToolInputError(f"#{todo_id} 할 일이 없습니다.")
        return todo


def _raise(message: str):
    raise ToolInputError(message)
