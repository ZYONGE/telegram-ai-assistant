"""시스템 프롬프트 조립: 템플릿 + 프로필 + 기억 + 이전 대화 요약.

현재 시각처럼 매번 바뀌는 값은 시스템 프롬프트에 넣지 않는다 (프롬프트 캐시가 깨진다).
시각은 사용자 메시지 앞에 붙인다.
사용자 이름·호칭은 저장소에 두지 않고, git에서 제외된 private/profile.md에서 읽는다.
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
    """작성 안내 주석과 비어 있는 항목을 빼서 모델에 넘길 내용만 남긴다."""
    lines = [
        line.rstrip()
        for line in _COMMENT.sub("", text).splitlines()
        if not _EMPTY_FIELD.match(line) and not _EMPTY_ROW.match(line) and line.strip() != "-"
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def format_now(now: datetime) -> str:
    local = to_kst(now)
    return f"{local:%Y-%m-%d}({_WEEKDAYS[local.weekday()]}) {local:%H:%M} (Asia/Seoul)"


class PromptBuilder:
    def __init__(self, template_path: Path, profile_path: Path, memory: MemoryStore) -> None:
        self._template_path = template_path
        self._profile_path = profile_path
        self._memory = memory

    async def build(self, summary: str) -> str:
        template = await asyncio.to_thread(self._template_path.read_text, encoding="utf-8")
        raw_profile = await asyncio.to_thread(self._read_profile)
        items = await self._memory.read()
        memory = "\n".join(f"- [{item.item_id}] {item.text}" for item in items) or "(아직 없음)"
        return (
            _COMMENT.sub("", template)
            .replace("{honorific}", parse_identity(raw_profile).honorific)
            .replace("{profile}", clean_profile(raw_profile) or "(아직 작성되지 않음)")
            .replace("{memory}", memory)
            .replace("{summary}", summary.strip() or "(없음)")
            .strip()
        )

    def _read_profile(self) -> str:
        if not self._profile_path.exists():
            return ""
        return self._profile_path.read_text(encoding="utf-8")
