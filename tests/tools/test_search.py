import httpx
import pytest
from google.genai import errors as genai_errors
from google.genai import types

from app.llm import SearchResult
from app.llm.base import LLMError, TransientLLMError
from app.llm.gemini import GeminiSearch, grounding_sources
from app.tools.search import BUSY_MESSAGE, DISABLED_MESSAGE, EMPTY_MESSAGE, FAILED_MESSAGE, HEADER, search_tools
from tests.conftest import FakeGenAI, blocked_prompt_response, server_error, text_part


def grounded(text: str, *sources: tuple[str, str]) -> types.GenerateContentResponse:
    chunks = [types.GroundingChunk(web=types.GroundingChunkWeb(title=title, uri=uri)) for title, uri in sources]
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[text_part(text)]),
                finish_reason="STOP",
                grounding_metadata=types.GroundingMetadata(grounding_chunks=chunks),
            )
        ]
    )


class FakeSearch:
    def __init__(self, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.queries: list[str] = []

    async def search(self, query: str, *, max_tokens: int = 800) -> SearchResult:
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.result


async def test_gemini_search_asks_for_google_search_and_returns_sources():
    client = FakeGenAI(grounded("내일은 맑습니다.", ("기상청", "https://example.com/a")))
    result = await GeminiSearch(client, "light-m").search("내일 날씨")

    config = client.models.calls[0]["config"]
    assert config.tools[0].google_search is not None
    assert client.models.calls[0]["model"] == "light-m"
    assert result.text == "내일은 맑습니다."
    assert result.sources == ("기상청 https://example.com/a",)


def test_grounding_sources_are_limited_and_tolerate_missing_fields():
    response = grounded("본문", *[(f"제목{i}", f"https://example.com/{i}") for i in range(7)])
    assert len(grounding_sources(response)) == 5
    assert grounding_sources(grounded("본문")) == []
    assert grounding_sources(blocked_prompt_response()) == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (server_error(), TransientLLMError),
        (genai_errors.ClientError(429, {"error": {"code": 429, "message": "quota"}}), TransientLLMError),
        (genai_errors.ClientError(400, {"error": {"code": 400, "message": "grounding not supported"}}), LLMError),
        (httpx.ConnectError("down"), TransientLLMError),
    ],
)
async def test_search_errors_are_wrapped_without_details(error, expected):
    with pytest.raises(expected) as info:
        await GeminiSearch(FakeGenAI(error), "m").search("질문")
    assert type(info.value) is expected
    assert "quota" not in str(info.value) and "grounding not supported" not in str(info.value)


async def test_blocked_search_is_an_error():
    with pytest.raises(LLMError, match="차단"):
        await GeminiSearch(FakeGenAI(blocked_prompt_response()), "m").search("질문")


async def test_tool_marks_result_as_outside_data_and_lists_sources():
    fake = FakeSearch(SearchResult("모집은 9월 30일까지입니다.", ("공고 https://example.com/a",)))
    tool = search_tools(fake)[0]
    result = await tool.run({"query": "교환학생 모집 마감"})

    assert fake.queries == ["교환학생 모집 마감"]
    assert result.is_error is False
    assert result.content.startswith(HEADER)
    assert "모집은 9월 30일까지입니다." in result.content
    assert result.content.endswith("출처\n· 공고 https://example.com/a")


@pytest.mark.parametrize(
    ("search", "message"),
    [
        (None, DISABLED_MESSAGE),
        (FakeSearch(error=TransientLLMError("한도")), BUSY_MESSAGE),
        (FakeSearch(error=LLMError("모델 오류")), FAILED_MESSAGE),
        (FakeSearch(SearchResult("  ")), EMPTY_MESSAGE),
    ],
)
async def test_tool_failures_are_short_and_marked_as_errors(search, message):
    result = await search_tools(search)[0].run({"query": "무엇"})
    assert result.is_error is True and result.content == message


async def test_tool_requires_a_query():
    from app.tools.common import ToolInputError

    with pytest.raises(ToolInputError):
        await search_tools(FakeSearch(SearchResult("x")))[0].run({})
