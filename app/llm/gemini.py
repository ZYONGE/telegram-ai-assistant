"""Gemini 어댑터 (Google Gen AI SDK).

호출 방식 ([llm.gemini] backend)
- "api_key": Gemini API 키 (GEMINI_API_KEY). 목표는 Cloud 결제 계정을 연결한 프로젝트의 키(B안)다.
  무료 티어 키는 입력 내용이 제품 개선에 쓰일 수 있으므로, billing_enabled = true이거나
  무료 티어 사용을 명시적으로 허용(allow_free_tier = true)해야 시작한다.
- "vertex": 같은 프로젝트의 Vertex AI (서비스 계정 인증, GOOGLE_APPLICATION_CREDENTIALS).

자동 함수 호출은 끈다. 도구 실행과 확인 단계는 우리 레지스트리가 맡는다.
모델 응답 Content를 그대로 저장·재전송해 생각 서명(thought_signature)을 보존한다.
"""

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
from google import genai
from google.auth.exceptions import DefaultCredentialsError
from google.genai import errors as genai_errors
from google.genai import types

from app.core.config import ConfigError, LLMSettings
from app.core.interfaces import ToolResult
from app.llm.base import LLM, Finish, LLMError, ModelTurn, SearchResult, ToolCall, TransientLLMError, Turn

logger = logging.getLogger(__name__)

BACKENDS = ("api_key", "vertex")
_BLOCKED = {
    types.FinishReason.SAFETY,
    types.FinishReason.RECITATION,
    types.FinishReason.BLOCKLIST,
    types.FinishReason.PROHIBITED_CONTENT,
    types.FinishReason.SPII,
}
_TOOL_RESULT_LIMIT = 300
_MAX_SOURCES = 5

# 웹 검색용 별도 호출. 대화 루프의 도구 호출과 섞지 않으려고 따로 부른다.
SEARCH_SYSTEM = (
    "웹 검색 결과를 바탕으로 질문에 한국어로 간결하게 답한다. "
    "확인된 내용만 적고, 확실하지 않으면 모른다고 적는다. 다섯 문장을 넘기지 않는다."
)


async def create(settings: LLMSettings, client_factory: Callable[..., Any] = genai.Client) -> LLM:
    client = _create_client(settings.options, client_factory)
    try:
        await verify_models(client, [settings.chat_model, settings.light_model])
    except Exception:
        await client.aio.aclose()
        raise
    # 웹 검색은 Google 검색 그라운딩을 쓴다. 끄려면 [llm.gemini] web_search = false.
    search = GeminiSearch(client, settings.light_model) if settings.options.get("web_search", True) else None
    return LLM(
        chat=GeminiModel(client, settings.chat_model),
        light=GeminiModel(client, settings.light_model),
        close=client.aio.aclose,
        search=search,
    )


def _create_client(options: dict[str, Any], client_factory: Callable[..., Any]) -> Any:
    backend = options.get("backend", "api_key")
    if backend == "api_key":
        if options.get("billing_enabled") is not True:
            if options.get("allow_free_tier") is not True:
                raise ConfigError(
                    "Gemini 무료 티어는 입력 내용이 제품 개선에 쓰일 수 있습니다. "
                    "결제 계정을 연결한 프로젝트의 키를 쓰고 [llm.gemini] billing_enabled = true로 바꾸거나, "
                    "무료 티어를 감수한다면 allow_free_tier = true로 바꾸세요."
                )
            logger.warning(
                "Gemini 무료 티어로 실행합니다. 대화·프로필·할 일 내용이 Google 제품 개선에 쓰일 수 있습니다. "
                "결제를 연결하면 billing_enabled = true, allow_free_tier = false로 바꾸세요."
            )
        try:
            # API 키는 SDK가 환경변수 GEMINI_API_KEY에서 읽는다 (private/.env는 load_settings가 불러 둠)
            return client_factory(vertexai=False)
        except ValueError:
            raise ConfigError("GEMINI_API_KEY가 설정되지 않았습니다. private/.env에 추가하세요.") from None
    if backend == "vertex":
        project = options.get("project")
        if not project:
            raise ConfigError("[llm.gemini] backend가 vertex이면 project(Google Cloud 프로젝트 ID)가 필요합니다.")
        try:
            return client_factory(vertexai=True, project=project, location=options.get("location", "global"))
        except (DefaultCredentialsError, ValueError):
            raise ConfigError(
                "Vertex AI 인증 정보를 찾지 못했습니다. private/.env의 GOOGLE_APPLICATION_CREDENTIALS를 확인하세요."
            ) from None
    raise ConfigError(f"[llm.gemini] backend는 {BACKENDS} 중 하나여야 합니다: {backend!r}")


async def verify_models(client: Any, names: list[str]) -> None:
    """시작할 때 인증과 모델 ID를 확인해, 첫 대화에서야 실패하는 일을 막는다."""
    for name in dict.fromkeys(names):
        try:
            await client.aio.models.get(model=name)
        except genai_errors.ClientError as exc:
            raise ConfigError(
                f"Gemini API 확인 실패(HTTP {exc.code}): 모델 ID '{name}'와 인증 정보를 확인하세요."
            ) from None


class GeminiModel:
    def __init__(self, client: Any, model: str) -> None:
        self._client = client
        self.model = model

    async def generate(
        self, system: str, history: list[Turn], tools: list[dict[str, Any]], *, max_tokens: int
    ) -> ModelTurn:
        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            tools=[types.Tool(function_declarations=[_declaration(tool) for tool in tools])] if tools else None,
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self.model,
                contents=[types.Content.model_validate(turn) for turn in merge_same_role(history)],
                config=config,
            )
        except (genai_errors.APIError, httpx.TransportError) as exc:
            raise as_llm_error(exc) from exc
        return to_model_turn(response)

    def user_turn(self, texts: list[str]) -> Turn:
        return {"role": "user", "parts": [{"text": text} for text in texts]}

    def tool_results_turn(self, results: list[tuple[ToolCall, ToolResult]]) -> Turn:
        parts = []
        for call, result in results:
            response = {"error": result.content} if result.is_error else {"result": result.content}
            function_response: dict[str, Any] = {"name": call.name, "response": response}
            if call.id:
                function_response["id"] = call.id
            parts.append({"function_response": function_response})
        return {"role": "user", "parts": parts}

    def pending_tool_calls(self, content: Turn) -> list[ToolCall]:
        if content.get("role") != "model":
            return []
        return [_tool_call(part["function_call"]) for part in content.get("parts", []) if "function_call" in part]

    def render(self, role: str, content: Turn) -> list[str]:
        lines = []
        for part in content.get("parts", []):
            if part.get("thought"):
                continue
            if "text" in part:
                speaker = "사용자" if role == "user" else "비서"
                lines.append(f"{speaker}: {part['text']}")
            elif "function_call" in part:
                call = part["function_call"]
                lines.append(f"비서 도구 호출: {call['name']} {json.dumps(call.get('args', {}), ensure_ascii=False)}")
            elif "function_response" in part:
                response = part["function_response"].get("response", {})
                value = response.get("result", response.get("error", response))
                if not isinstance(value, str):
                    value = json.dumps(value, ensure_ascii=False)
                lines.append(f"도구 결과: {value[:_TOOL_RESULT_LIMIT]}")
        return lines


class GeminiSearch:
    """Google 검색 그라운딩으로 최신 정보를 찾는다. 결과 문장과 출처만 돌려준다."""

    def __init__(self, client: Any, model: str) -> None:
        self._client = client
        self.model = model

    async def search(self, query: str, *, max_tokens: int = 800) -> SearchResult:
        config = types.GenerateContentConfig(
            system_instruction=SEARCH_SYSTEM,
            max_output_tokens=max_tokens,
            tools=[types.Tool(google_search=types.GoogleSearch())],
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self.model,
                contents=[types.Content(role="user", parts=[types.Part(text=query)])],
                config=config,
            )
        except (genai_errors.APIError, httpx.TransportError) as exc:
            raise as_llm_error(exc) from exc
        turn = to_model_turn(response)
        if turn.finish is Finish.BLOCKED:
            raise LLMError("검색 결과가 안전 정책으로 차단되었습니다.")
        return SearchResult(text=turn.text, sources=tuple(grounding_sources(response)))


def grounding_sources(response: types.GenerateContentResponse) -> list[str]:
    """그라운딩에 쓰인 출처. 사용자에게 함께 보여 준다."""
    candidates = response.candidates or []
    metadata = candidates[0].grounding_metadata if candidates else None
    sources = []
    for chunk in (metadata.grounding_chunks if metadata else None) or []:
        web = getattr(chunk, "web", None)
        if web and web.uri:
            sources.append(f"{web.title} {web.uri}" if web.title else web.uri)
    return sources[:_MAX_SOURCES]


def as_llm_error(exc: Exception) -> LLMError:
    """제공사 예외를 공통 오류로 바꾼다. 메시지에 요청 내용이나 비밀값을 넣지 않는다."""
    if isinstance(exc, genai_errors.ServerError):
        return TransientLLMError(f"Gemini 서버 오류 (HTTP {exc.code})")
    if isinstance(exc, genai_errors.ClientError):
        if exc.code == 429:
            return TransientLLMError("Gemini 요청 한도 초과 (HTTP 429)")
        return LLMError(f"Gemini 요청 오류 (HTTP {exc.code})")
    if isinstance(exc, genai_errors.APIError):
        return LLMError(f"Gemini 오류 (HTTP {exc.code})")
    return TransientLLMError(f"Gemini 연결 실패 ({type(exc).__name__})")


def to_model_turn(response: types.GenerateContentResponse) -> ModelTurn:
    if not response.candidates:
        # 요청 자체가 차단된 경우 (prompt_feedback.block_reason)
        return ModelTurn(content=None, text="", finish=Finish.BLOCKED)
    candidate = response.candidates[0]
    parts = candidate.content.parts if candidate.content and candidate.content.parts else []
    text = "".join(part.text for part in parts if part.text and not part.thought)
    calls = [_tool_call(part.function_call.model_dump(exclude_none=True)) for part in parts if part.function_call]

    if calls:
        finish = Finish.TOOL_CALLS
    elif candidate.finish_reason == types.FinishReason.MAX_TOKENS:
        finish = Finish.MAX_TOKENS
    elif candidate.finish_reason in _BLOCKED:
        finish = Finish.BLOCKED
    else:
        finish = Finish.STOP

    content = None
    if parts:
        content = candidate.content.model_dump(mode="json", exclude_none=True)
        content["role"] = "model"
    return ModelTurn(content=content, text=text, finish=finish, tool_calls=calls)


def merge_same_role(history: list[Turn]) -> list[Turn]:
    """같은 역할이 이어진 턴을 합친다 (오류로 모델 응답이 빠진 경우 대비)."""
    merged: list[Turn] = []
    for turn in history:
        if merged and merged[-1]["role"] == turn["role"]:
            merged[-1] = {"role": turn["role"], "parts": [*merged[-1]["parts"], *turn["parts"]]}
        else:
            merged.append(turn)
    return merged


def _declaration(tool: dict[str, Any]) -> types.FunctionDeclaration:
    return types.FunctionDeclaration(
        name=tool["name"],
        description=tool["description"],
        parameters_json_schema=tool["input_schema"],
    )


def _tool_call(call: dict[str, Any]) -> ToolCall:
    return ToolCall(id=call.get("id"), name=call["name"], args=dict(call.get("args") or {}))
