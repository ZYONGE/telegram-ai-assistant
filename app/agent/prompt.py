"""시스템 프롬프트 조립: 템플릿 + 지시 + 프로필 + 기억 + 이전 대화 요약.

현재 시각처럼 매번 바뀌는 값은 시스템 프롬프트에 넣지 않는다 (프롬프트 캐시가 깨진다).
시각은 사용자 메시지 앞에 붙인다.
사용자 이름·호칭은 저장소에 두지 않고, git에서 제외된 private/profile.md에서 읽는다.
말투·보고 방법 같은 판단 기준은 private/instructions.md에서 읽는다. 둘 다 없으면 기본값으로 동작한다.
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.core.clock import to_kst
from app.core.interfaces import MemoryStore

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_EMPTY_FIELD = re.compile(r"^\s*-\s*[^:]+:\s*$")
_EMPTY_ROW = re.compile(r"^\s*\|(\s*\|)+\s*$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_IDENTITY_FIELD = re.compile(r"^\s*-\s*(이름|호칭)\s*:\s*(\S.*?)\s*$", re.MULTILINE)
_WEEKDAYS = "월화수목금토일"
DEFAULT_HONORIFIC = "사용자님"


@dataclass(frozen=True, slots=True)
class UserIdentity:
    name: str = ""
    # 비서가 사용자를 부르는 말 (예: 홍길동 → "길동님")
    honorific: str = DEFAULT_HONORIFIC


def parse_identity(profile_text: str) -> UserIdentity:
    fields = dict(_IDENTITY_FIELD.findall(_COMMENT.sub("", profile_text)))
    name = fields.get("이름", "")
    honorific = fields.get("호칭") or (f"{name}님" if name else DEFAULT_HONORIFIC)
    return UserIdentity(name=name, honorific=honorific)


def load_identity(profile_path: Path) -> UserIdentity:
    if not profile_path.exists():
        return UserIdentity()
    return parse_identity(profile_path.read_text(encoding="utf-8"))


def clean_profile(text: str) -> str:
    """작성 안내 주석과 비어 있는 항목·표·절을 빼서 모델에 넘길 내용만 남긴다."""
    lines = [
        line.rstrip()
        for line in _COMMENT.sub("", text).splitlines()
        if not _EMPTY_FIELD.match(line) and not _EMPTY_ROW.match(line) and line.strip() != "-"
    ]
    lines = drop_empty_sections(drop_empty_tables(lines))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def drop_empty_tables(lines: list[str]) -> list[str]:
    """내용 행이 하나도 남지 않은 표는 머리글까지 지운다."""
    kept: list[str] = []
    for index, line in enumerate(lines):
        following = lines[index + 1].lstrip() if index + 1 < len(lines) else ""
        is_header = kept and kept[-1].lstrip().startswith("|")
        if _TABLE_SEPARATOR.match(line) and is_header and not following.startswith("|"):
            kept.pop()  # 머리글 줄까지 지운다
            continue
        kept.append(line)
    return kept


def drop_empty_sections(lines: list[str]) -> list[str]:
    """내용이 없는 제목은 지운다. 하위 절에 내용이 남아 있으면 상위 제목은 남긴다."""
    root: dict = {"level": 0, "heading": None, "body": [], "children": []}
    stack = [root]
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            while len(stack) > 1 and stack[-1]["level"] >= level:
                stack.pop()
            node: dict = {"level": level, "heading": line, "body": [], "children": []}
            stack[-1]["children"].append(node)
            stack.append(node)
        else:
            stack[-1]["body"].append(line)
    return _render_section(root)


def _render_section(node: dict) -> list[str]:
    children: list[str] = []
    for child in node["children"]:
        children += _render_section(child)
    if not any(line.strip() for line in node["body"]) and not children:
        return []
    if node["heading"] is None:
        return [*node["body"], *children]
    return [node["heading"], *node["body"], *children]


def format_now(now: datetime) -> str:
    local = to_kst(now)
    return f"{local:%Y-%m-%d}({_WEEKDAYS[local.weekday()]}) {local:%H:%M} (Asia/Seoul)"


class PromptBuilder:
    def __init__(
        self,
        template_path: Path,
        profile_path: Path,
        memory: MemoryStore,
        instructions_path: Path | None = None,
    ) -> None:
        self._template_path = template_path
        self._profile_path = profile_path
        self._memory = memory
        # 사용자가 직접 적은 판단 기준 (말투, 보고 방법, 일정·메일 방침)
        self._instructions_path = instructions_path

    async def build(self, summary: str) -> str:
        template = await asyncio.to_thread(self._template_path.read_text, encoding="utf-8")
        raw_profile = await asyncio.to_thread(self._read, self._profile_path)
        raw_instructions = await asyncio.to_thread(self._read, self._instructions_path)
        items = await self._memory.read()
        memory = "\n".join(f"- [{item.item_id}] {item.text}" for item in items) or "(아직 없음)"
        return (
            _COMMENT.sub("", template)
            .replace("{honorific}", parse_identity(raw_profile).honorific)
            .replace("{instructions}", clean_profile(raw_instructions) or "(아직 작성되지 않음)")
            .replace("{profile}", clean_profile(raw_profile) or "(아직 작성되지 않음)")
            .replace("{memory}", memory)
            .replace("{summary}", summary.strip() or "(없음)")
            .strip()
        )

    @staticmethod
    def _read(path: Path | None) -> str:
        if path is None or not path.exists():
            return ""
        return path.read_text(encoding="utf-8")
