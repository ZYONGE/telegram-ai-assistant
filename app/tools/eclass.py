"""eClass 도구: 수집 범위 보기와 바꾸기.

"게시판은 알리지 마" 같은 말을 받아 화면의 처리 수준을 바꾼다.
- 조회는 바로, 바꾸기는 확인 버튼을 거친다 (CLAUDE.md 7절).
- 사용자가 바꾼 것은 다시 정할 때 덮어쓰지 않는다.
- 과제 제출·글쓰기 같은 것은 만들지 않는다. 조회 범위만 다룬다 (절대 규칙 5).
"""

from collections.abc import Mapping
from typing import Any

from app.collectors.eclass.scope import LABELS, Decided, ScopeEntry, ScopeStore, collected
from app.core.config import Level
from app.core.interfaces import Confirmation, ToolResult
from app.tools.common import SimpleTool, ToolInputError, require_str, spec

LEVEL_HELP = " · ".join(f"{level}({LABELS[level]})" for level in Level)
NO_SCOPE = (
    "아직 eClass 화면 목록을 만들지 않았습니다. "
    "`scripts/eclass_explore.py`를 한 번 돌리면 화면마다 다룰 수준이 정해집니다."
)


def format_entry(entry: ScopeEntry) -> str:
    parts = [f"{entry.name or entry.path} [{LABELS[entry.level]}]"]
    if entry.per_course:
        parts.append("과목별")
    if entry.by is Decided.USER:
        parts.append("직접 정하심")
    return " · ".join(parts)


def find(scope: dict[str, ScopeEntry], needle: str) -> list[ScopeEntry]:
    """이름이나 경로로 화면을 찾는다. 사용자가 경로를 외울 필요가 없게 한다."""
    lowered = needle.strip().lower()
    if not lowered:
        return []
    exact = [entry for entry in scope.values() if entry.path.lower() == lowered]
    if exact:
        return exact
    return [
        entry
        for entry in scope.values()
        if lowered in entry.name.lower() or lowered in entry.path.lower()
    ]


def eclass_tools(store: ScopeStore) -> list:
    async def show_scope(args: Mapping[str, Any]) -> ToolResult:
        scope = store.read()
        if not scope:
            return ToolResult(NO_SCOPE)
        lines = [format_entry(entry) for entry in collected(scope)]
        off = sum(1 for entry in scope.values() if entry.level is Level.OFF)
        if not lines:
            return ToolResult(f"지금은 eClass에서 가져오는 화면이 없습니다. 꺼 둔 화면 {off}개.")
        return ToolResult("\n".join(lines) + (f"\n그 밖에 꺼 둔 화면 {off}개" if off else ""))

    def _pick(args: Mapping[str, Any]) -> tuple[ScopeEntry, Level]:
        scope = store.read()
        if not scope:
            raise ToolInputError(NO_SCOPE)
        needle = require_str(args, "screen", max_len=100)
        raw = require_str(args, "level", max_len=20).strip().lower()
        try:
            level = Level(raw)
        except ValueError as exc:
            raise ToolInputError(f"수준은 {LEVEL_HELP} 중 하나여야 합니다.") from exc

        found = find(scope, needle)
        if not found:
            raise ToolInputError(f"'{needle}'에 해당하는 eClass 화면을 찾지 못했습니다.")
        if len(found) > 1:
            names = ", ".join(entry.name or entry.path for entry in found[:5])
            raise ToolInputError(f"'{needle}'에 해당하는 화면이 여럿입니다: {names}. 하나만 골라 주세요.")
        return found[0], level

    async def describe_change(args: Mapping[str, Any]) -> str:
        entry, level = _pick(args)
        name = entry.name or entry.path
        if entry.level is level:
            return f"eClass '{name}'은 이미 {LABELS[level]}입니다."
        return f"eClass '{name}'을 {LABELS[entry.level]} → {LABELS[level]}으로 바꿀까요?"

    async def set_scope(args: Mapping[str, Any]) -> ToolResult:
        entry, level = _pick(args)
        saved = store.set_level(entry.path, level)
        name = saved.name or entry.name or saved.path
        return ToolResult(f"eClass '{name}'을 {LABELS[level]}으로 바꿨습니다.")

    return [
        SimpleTool(
            spec(
                "eclass_scope",
                "eClass에서 무엇을 가져오고 무엇을 알리는지 보여 준다. "
                f"화면마다 수준이 있다: {LEVEL_HELP}.",
                {},
                [],
            ),
            show_scope,
        ),
        SetScopeTool(
            spec(
                "eclass_scope_set",
                "eClass 화면 하나의 처리 수준을 바꾼다. "
                "'공지는 바로 알려 줘', '게시판은 알리지 마' 같은 요청에 쓴다.",
                {
                    "screen": {
                        "type": "string",
                        "description": "화면 이름이나 경로. eclass_scope가 보여 준 이름을 그대로 쓴다.",
                    },
                    "level": {
                        "type": "string",
                        "enum": [str(level) for level in Level],
                        "description": f"처리 수준. {LEVEL_HELP}",
                    },
                },
                ["screen", "level"],
                confirmation=Confirmation.BUTTON,
            ),
            set_scope,
            describe_change,
        ),
    ]


class SetScopeTool:
    """수준 바꾸기. 확인 문구에 지금 수준과 바뀔 수준을 함께 보여 준다."""

    def __init__(self, tool_spec, run, describe) -> None:
        self.spec = tool_spec
        self._run = run
        self._describe = describe

    async def run(self, args: Mapping[str, Any]) -> ToolResult:
        return await self._run(args)

    async def describe(self, args: Mapping[str, Any]) -> str:
        return await self._describe(args)
