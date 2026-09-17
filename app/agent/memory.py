"""장기 기억: 비서가 대화 중 기록하는 지속 정보 (nanobot의 MEMORY.md 역할, docs/adr/0003).

마크다운 파일 한 개에 한 줄씩 기록한다.
    - [m-1a2b3c] (2026-09-18) 월요일 오전에는 수업이 없음
"""

import asyncio
import re
import secrets
from datetime import datetime
from pathlib import Path

from app.core.clock import to_kst
from app.core.interfaces import MemoryItem

_HEADER = "# 기억\n\n<!-- 비서가 기록합니다. 직접 고쳐도 되지만 한 줄 형식은 유지해 주세요. -->\n\n"
_LINE = re.compile(r"^- \[(?P<id>m-[0-9a-f]+)\] \((?P<date>\d{4}-\d{2}-\d{2})\) (?P<text>.+)$")

# 기록하면 안 되는 정보 (CLAUDE.md 7절)
_SENSITIVE = [
    (re.compile(r"\d{6}\s*-\s*[1-8]\d{6}"), "주민등록번호"),
    (re.compile(r"(?:\d{4}[\s-]?){3}\d{4}"), "카드 번호"),
    # 마지막 묶음을 4자리 이상으로 제한해 2026-09-20 같은 날짜는 통과시킨다
    (re.compile(r"\b\d{2,6}-\d{2,6}-\d{4,8}\b"), "계좌 번호"),
    (re.compile(r"비밀\s*번호|패스워드|password|passwd|\bpw\b|OTP|CVC|CVV", re.IGNORECASE), "비밀번호"),
    (re.compile(r"여권\s*번호|운전\s*면허\s*번호|외국인\s*등록\s*번호"), "신분증 번호"),
]


def sensitive_reason(text: str) -> str | None:
    for pattern, label in _SENSITIVE:
        if pattern.search(text):
            return label
    return None


class MarkdownMemoryStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def read(self) -> list[MemoryItem]:
        async with self._lock:
            return await asyncio.to_thread(self._read)

    async def add(self, text: str, now: datetime | None = None) -> MemoryItem:
        text = " ".join(text.split())
        if not text:
            raise ValueError("빈 내용은 기억할 수 없습니다")
        reason = sensitive_reason(text)
        if reason:
            raise ValueError(f"{reason}로 보이는 정보는 기록하지 않습니다")
        created = to_kst(now) if now else datetime.now().astimezone()
        item = MemoryItem(f"m-{secrets.token_hex(3)}", text, created)
        async with self._lock:
            items = await asyncio.to_thread(self._read)
            await asyncio.to_thread(self._write, [*items, item])
        return item

    async def delete(self, item_id: str) -> bool:
        async with self._lock:
            items = await asyncio.to_thread(self._read)
            kept = [item for item in items if item.item_id != item_id]
            if len(kept) == len(items):
                return False
            await asyncio.to_thread(self._write, kept)
            return True

    def _read(self) -> list[MemoryItem]:
        if not self._path.exists():
            return []
        items = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            match = _LINE.match(line.strip())
            if match:
                created = datetime.fromisoformat(match["date"])
                items.append(MemoryItem(match["id"], match["text"], created))
        return items

    def _write(self, items: list[MemoryItem]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"- [{item.item_id}] ({item.created_at:%Y-%m-%d}) {item.text}" for item in items]
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(_HEADER + "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        tmp.replace(self._path)
