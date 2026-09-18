import httpx
import pytest

from app.collectors.webpage import PageUnavailable, check_url, extract, fetch_page
from app.core.interfaces import Confirmation
from app.storage.archive import ArchiveRepository
from app.tools.archive import archive_tools
from app.tools.common import ToolInputError
from tests.conftest import kst

PAGE = """
<html><head><title> 교환학생 모집 공고 </title>
<style>.a{color:red}</style></head>
<body><script>alert(1)</script>
<h1>2027학년도 1학기 교환학생</h1>
<p>신청 기간은 9월 30일까지입니다.</p>
<p>제출 서류 &amp; 어학 성적을 확인하세요.</p>
</body></html>
"""


class FakeSummarizer:
    def __init__(self, summary="9월 30일까지 교환학생 신청을 받는다.") -> None:
        self.summary = summary
        self.seen: list[tuple[str, str]] = []

    async def summarize_page(self, title: str, text: str) -> str:
        self.seen.append((title, text))
        return self.summary


@pytest.fixture
def archive(db):
    return ArchiveRepository(db)


def page_client(response: httpx.Response | None = None) -> httpx.AsyncClient:
    reply = response or httpx.Response(200, text=PAGE, headers={"content-type": "text/html; charset=utf-8"})
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: reply))


def tools(archive, client, summarizer=None, clock=None):
    return archive_tools(archive, client, summarizer, clock or (lambda: kst(9, 18, 14)))


def tool(archive, name, client=None, summarizer=None):
    found = [t for t in tools(archive, client or page_client(), summarizer) if t.spec.name == name]
    return found[0]


# --- 주소 검사와 본문 추출 ---


def test_check_url_adds_scheme_and_blocks_internal_addresses():
    assert check_url("example.com/a") == "https://example.com/a"
    assert check_url("http://example.com") == "http://example.com"
    for blocked in ("localhost:8000", "http://127.0.0.1/x", "https://192.168.0.5", "http://nas.local"):
        with pytest.raises(PageUnavailable, match="내부망"):
            check_url(blocked)


def test_check_url_rejects_unusable_address():
    with pytest.raises(PageUnavailable):
        check_url("https://")


def test_extract_drops_scripts_and_keeps_text():
    title, text = extract(PAGE)
    assert title == "교환학생 모집 공고"
    assert "alert(1)" not in text and "color:red" not in text
    assert "2027학년도 1학기 교환학생" in text
    assert "신청 기간은 9월 30일까지입니다." in text
    assert "제출 서류 & 어학 성적" in text


async def test_fetch_page_reads_title_and_body():
    async with page_client() as client:
        page = await fetch_page(client, "example.com/notice")
    assert page.title == "교환학생 모집 공고"
    assert "9월 30일" in page.text


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(404, text="없음"), "HTTP 404"),
        (httpx.Response(200, text="%PDF-1.4", headers={"content-type": "application/pdf"}), "글이 아닌 파일"),
        (httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"}), "읽을 내용을 찾지 못했"),
    ],
)
async def test_fetch_page_failures_are_short(response, message):
    async with page_client(response) as client:
        with pytest.raises(PageUnavailable, match=message):
            await fetch_page(client, "https://example.com")


async def test_redirect_into_internal_address_is_blocked():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PageUnavailable, match="내부망"):
            await fetch_page(client, "http://127.0.0.1/x")


async def test_fetch_page_connection_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PageUnavailable, match="연결하지 못했습니다"):
            await fetch_page(client, "https://example.com")


# --- 저장소 ---


async def test_repository_search_matches_all_words_and_orders_newest_first(archive):
    await archive.add("note", "교환학생 준비", kst(9, 17, 10), body="어학 성적 정리")
    await archive.add("link", "인턴 공고", kst(9, 17, 11), url="https://example.com/a", summary="베를린 인턴")
    await archive.add("note", "교환학생 서류", kst(9, 17, 12), tags="교환학생")

    assert [i.id for i in await archive.search("교환학생")] == [3, 1]
    assert [i.id for i in await archive.search("어학 성적")] == [1]
    assert [i.id for i in await archive.search("베를린")] == [2]
    assert await archive.search("없는낱말") == []
    assert [i.id for i in await archive.search("")] == [3, 2, 1]


async def test_repository_limit_get_and_delete(archive):
    for index in range(3):
        await archive.add("note", f"메모 {index}", kst(9, 17, 10))
    assert len(await archive.list_recent(2)) == 2
    assert (await archive.get(1)).title == "메모 0"
    assert await archive.delete(1) is True
    assert await archive.delete(1) is False
    assert await archive.get(1) is None


async def test_repository_rejects_unknown_kind(archive):
    with pytest.raises(ValueError):
        await archive.add("photo", "사진", kst(9, 17, 10))


# --- 도구 ---


async def test_save_link_stores_title_summary_and_url(archive):
    summarizer = FakeSummarizer()
    client = page_client()
    async with client:
        result = await tool(archive, "save_link", client, summarizer).run(
            {"url": "example.com/notice", "note": "교환학생"}
        )

    assert "보관함에 저장했습니다." in result.content
    item = await archive.get(1)
    assert item.kind == "link" and item.title == "교환학생 모집 공고"
    assert item.url == "https://example.com/notice"
    assert item.summary == summarizer.summary and item.tags == "교환학생"
    assert "9월 30일" in item.body
    # 요약 모델에는 페이지 본문이 그대로 넘어간다 (외부 데이터로 다룬다)
    assert summarizer.seen[0][0] == "교환학생 모집 공고"


async def test_save_link_without_summarizer_keeps_user_note(archive):
    client = page_client()
    async with client:
        await tool(archive, "save_link", client).run({"url": "https://example.com", "note": "나중에 읽기"})
    assert (await archive.get(1)).summary == "나중에 읽기"


async def test_save_link_reports_page_failure_as_error(archive):
    client = page_client(httpx.Response(500, text="error"))
    async with client:
        result = await tool(archive, "save_link", client).run({"url": "https://example.com"})
    assert result.is_error is True and "HTTP 500" in result.content
    assert await archive.list_recent() == []


async def test_save_note_uses_first_line_as_title(archive):
    result = await tool(archive, "save_note").run({"text": "발표 아이디어\n자료 조사부터", "tags": "수업"})
    item = await archive.get(1)
    assert item.kind == "note" and item.title == "발표 아이디어"
    assert item.body.endswith("자료 조사부터") and item.tags == "수업"
    assert "#1 발표 아이디어" in result.content


async def test_search_and_open_tools(archive):
    await tool(archive, "save_note").run({"text": "교환학생 서류 목록", "title": "교환학생 서류"})
    found = await tool(archive, "search_archive").run({"query": "교환학생"})
    assert "#1 교환학생 서류" in found.content

    empty = await tool(archive, "search_archive").run({"query": "없는낱말"})
    assert empty.content == "보관함에서 찾지 못했습니다."

    opened = await tool(archive, "open_archive_item").run({"item_id": 1})
    assert "교환학생 서류 목록" in opened.content
    with pytest.raises(ToolInputError):
        await tool(archive, "open_archive_item").run({"item_id": 99})


async def test_delete_needs_confirmation(archive, registry):
    await tool(archive, "save_note").run({"text": "지울 메모"})
    client = page_client()
    async with client:
        registry.register(*tools(archive, client))
        outcome = await registry.call("delete_archive_item", {"item_id": 1}, kst(9, 18, 14))
        assert outcome.pending is not None
        assert outcome.pending.summary == "보관함 삭제 — #1 지울 메모"
        assert await archive.get(1) is not None

        action, result = await registry.confirm(outcome.pending.id, kst(9, 18, 14))
        assert "삭제했습니다" in result.content
        assert await archive.get(1) is None


def test_tool_confirmation_levels(archive):
    levels = {t.spec.name: t.spec.confirmation for t in tools(archive, page_client())}
    assert levels["save_link"] is Confirmation.IMMEDIATE
    assert levels["save_note"] is Confirmation.IMMEDIATE
    assert levels["search_archive"] is Confirmation.IMMEDIATE
    assert levels["delete_archive_item"] is Confirmation.BUTTON
