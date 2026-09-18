"""보관함 도구: 링크 요약 저장, 메모 저장, 말로 검색.

링크 본문과 요약은 외부에서 온 데이터다. 저장할 때도 지시로 다루지 않는다 (절대 규칙 8).
사진·음성은 저장하지 않는다 (CLAUDE.md 6절).
"""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Protocol

import httpx

from app.collectors.webpage import PageUnavailable, fetch_page
from app.core.clock import format_kst, utc_now
from app.core.interfaces import Confirmation, ToolResult
from app.storage.archive import ArchiveItem, ArchiveRepository
from app.tools.common import SimpleTool, ToolInputError, optional_str, require_int, require_str, spec

MAX_SEARCH_RESULTS = 10


class PageSummarizer(Protocol):
    async def summarize_page(self, title: str, text: str) -> str: ...


def format_item(item: ArchiveItem, *, full: bool = False) -> str:
    head = f"#{item.id} {item.title}"
    parts = [head]
    if item.url:
        parts.append(item.url)
    text = item.summary or item.body
    if text:
        parts.append(text if full else text.splitlines()[0][:120])
    parts.append(f"({format_kst(item.created_at)} 저장)")
    return "\n".join(parts)


def archive_tools(
    repo: ArchiveRepository,
    client: httpx.AsyncClient,
    summarizer: PageSummarizer | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> list:
    async def save_link(args: Mapping[str, Any]) -> ToolResult:
        url = require_str(args, "url", max_len=1000)
        note = optional_str(args, "note") or ""
        try:
            page = await fetch_page(client, url)
        except PageUnavailable as exc:
            return ToolResult(str(exc), is_error=True)

        summary = ""
        if summarizer is not None:
            summary = await summarizer.summarize_page(page.title, page.text)
        item = await repo.add(
            "link",
            page.title or page.url,
            clock(),
            url=page.url,
            summary=summary or note,
            body=page.text[:1000],
            tags=note,
        )
        return ToolResult(f"보관함에 저장했습니다.\n{format_item(item, full=True)}")

    async def save_note(args: Mapping[str, Any]) -> ToolResult:
        text = require_str(args, "text", max_len=2000)
        title = optional_str(args, "title") or text.splitlines()[0][:60]
        item = await repo.add("note", title, clock(), body=text, tags=optional_str(args, "tags") or "")
        return ToolResult(f"보관함에 저장했습니다.\n{format_item(item)}")

    async def search_archive(args: Mapping[str, Any]) -> ToolResult:
        query = optional_str(args, "query") or ""
        items = await repo.search(query, MAX_SEARCH_RESULTS)
        if not items:
            return ToolResult("보관함에서 찾지 못했습니다." if query else "보관함이 비어 있습니다.")
        return ToolResult("\n\n".join(format_item(item) for item in items))

    async def open_archive_item(args: Mapping[str, Any]) -> ToolResult:
        item = await repo.get(require_int(args, "item_id"))
        if item is None:
            raise ToolInputError(f"#{args.get('item_id')} 보관함 항목이 없습니다.")
        return ToolResult(format_item(item, full=True))

    return [
        SimpleTool(
            spec(
                "save_link",
                "링크를 보관함에 저장한다. 페이지를 읽어 제목과 요약을 함께 저장한다. "
                "사용자가 링크를 보내며 저장·정리를 요청하면 사용한다.",
                {
                    "url": {"type": "string", "description": "저장할 주소"},
                    "note": {"type": "string", "description": "사용자가 덧붙인 메모나 분류어 (선택)"},
                },
                ["url"],
            ),
            save_link,
        ),
        SimpleTool(
            spec(
                "save_note",
                "텍스트 메모를 보관함에 저장한다. 나중에 찾아볼 내용(아이디어, 정리한 정보, 인용)일 때 쓴다. "
                "사용자에 대한 지속적인 사실은 remember를 쓴다.",
                {
                    "text": {"type": "string", "description": "저장할 내용"},
                    "title": {"type": "string", "description": "짧은 제목 (선택)"},
                    "tags": {"type": "string", "description": "찾을 때 쓸 낱말들 (선택)"},
                },
                ["text"],
            ),
            save_note,
        ),
        SimpleTool(
            spec(
                "search_archive",
                "보관함에서 저장한 링크와 메모를 찾는다. 검색어를 비우면 최근 저장한 항목을 보여 준다.",
                {"query": {"type": "string", "description": "찾을 낱말 (띄어쓰기로 여러 개)"}},
                [],
            ),
            search_archive,
        ),
        SimpleTool(
            spec(
                "open_archive_item",
                "보관함 항목 하나를 자세히 본다 (요약 전문과 주소).",
                {"item_id": {"type": "integer", "description": "보관함 번호 (검색 결과의 # 뒤 숫자)"}},
                ["item_id"],
            ),
            open_archive_item,
        ),
        DeleteArchiveTool(repo),
    ]


class DeleteArchiveTool:
    """보관함 삭제는 확인 버튼을 받은 뒤 실행한다 (할 일 삭제와 같은 기준)."""

    def __init__(self, repo: ArchiveRepository) -> None:
        self._repo = repo
        self.spec = spec(
            "delete_archive_item",
            "보관함 항목을 삭제한다. 실행 전에 확인 버튼이 전송되고, 사용자가 확인해야 삭제된다.",
            {"item_id": {"type": "integer", "description": "보관함 번호"}},
            ["item_id"],
            confirmation=Confirmation.BUTTON,
        )

    async def describe(self, args: Mapping[str, Any]) -> str:
        item = await self._get(args)
        return f"보관함 삭제 — #{item.id} {item.title}"

    async def run(self, args: Mapping[str, Any]) -> ToolResult:
        item = await self._get(args)
        await self._repo.delete(item.id)
        return ToolResult(f"삭제했습니다: #{item.id} {item.title}")

    async def _get(self, args: Mapping[str, Any]) -> ArchiveItem:
        item_id = require_int(args, "item_id")
        item = await self._repo.get(item_id)
        if item is None:
            raise ToolInputError(f"#{item_id} 보관함 항목이 없습니다.")
        return item
