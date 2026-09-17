import httpx
import pytest
from google.auth.exceptions import DefaultCredentialsError
from google.genai import errors as genai_errors

from app.core.config import ConfigError, LLMSettings
from app.core.interfaces import ToolResult
from app.llm import Finish, LLMError, ToolCall, TransientLLMError
from app.llm import gemini
from app.llm.gemini import GeminiModel, merge_same_role, to_model_turn
from tests.conftest import FakeGenAI, blocked_prompt_response, call_part, gemini_response, server_error, text_part


def settings(**options):
    return LLMSettings(chat_model="chat-m", light_model="light-m", options=options)


class ClientFactory:
    def __init__(self, client=None, error=None):
        self.client = client or FakeGenAI()
        self.error = error
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.client


async def test_api_key_backend_requires_billing_confirmation():
    factory = ClientFactory()
    with pytest.raises(ConfigError, match="billing_enabled"):
        await gemini.create(settings(backend="api_key"), factory)
    assert factory.kwargs is None


async def test_api_key_backend_uses_developer_api_and_verifies_models():
    factory = ClientFactory()
    llm = await gemini.create(settings(billing_enabled=True), factory)
    assert factory.kwargs == {"vertexai": False}
    assert factory.client.models.checked == ["chat-m", "light-m"]
    assert llm.chat.model == "chat-m" and llm.light.model == "light-m"
    await llm.close()
    assert factory.client.closed


async def test_missing_api_key_is_config_error():
    factory = ClientFactory(error=ValueError("Missing key inputs argument"))
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        await gemini.create(settings(billing_enabled=True), factory)


async def test_unknown_model_is_config_error_and_client_is_closed():
    factory = ClientFactory(FakeGenAI(missing={"light-m"}))
    with pytest.raises(ConfigError, match="HTTP 404.*light-m"):
        await gemini.create(settings(billing_enabled=True), factory)
    assert factory.client.closed


async def test_same_model_is_verified_once():
    factory = ClientFactory()
    await gemini.create(LLMSettings(chat_model="m", light_model="m", options={"billing_enabled": True}), factory)
    assert factory.client.models.checked == ["m"]


async def test_vertex_backend_uses_project_and_location():
    factory = ClientFactory()
    await gemini.create(settings(backend="vertex", project="my-project", location="asia-northeast3"), factory)
    assert factory.kwargs == {"vertexai": True, "project": "my-project", "location": "asia-northeast3"}


@pytest.mark.parametrize(
    ("options", "error", "message"),
    [
        ({"backend": "vertex"}, None, "project"),
        ({"backend": "vertex", "project": "p"}, DefaultCredentialsError("no creds"), "GOOGLE_APPLICATION_CREDENTIALS"),
        ({"backend": "openai"}, None, "backend"),
    ],
)
async def test_invalid_backend_settings(options, error, message):
    with pytest.raises(ConfigError, match=message):
        await gemini.create(settings(**options), ClientFactory(error=error))


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (server_error(), TransientLLMError),
        (genai_errors.ClientError(429, {"error": {"code": 429, "message": "quota"}}), TransientLLMError),
        (genai_errors.ClientError(400, {"error": {"code": 400, "message": "bad"}}), LLMError),
        (httpx.ConnectError("down"), TransientLLMError),
    ],
)
async def test_sdk_errors_are_wrapped(error, expected):
    model = GeminiModel(FakeGenAI(error), "m")
    with pytest.raises(expected) as info:
        await model.generate("s", [model.user_turn(["hi"])], [], max_tokens=10)
    assert type(info.value) is expected
    assert "quota" not in str(info.value) and "bad" not in str(info.value)


async def test_generate_passes_tools_as_json_schema():
    client = FakeGenAI(gemini_response(text_part("ok")))
    model = GeminiModel(client, "m")
    tool = {"name": "add_todo", "description": "추가", "input_schema": {"type": "object", "properties": {}}}
    turn = await model.generate("시스템", [model.user_turn(["hi"])], [tool], max_tokens=123)

    config = client.models.calls[0]["config"]
    declaration = config.tools[0].function_declarations[0]
    assert declaration.name == "add_todo"
    assert declaration.parameters_json_schema == {"type": "object", "properties": {}}
    assert config.max_output_tokens == 123 and config.system_instruction == "시스템"
    assert turn.finish is Finish.STOP and turn.text == "ok"


def test_to_model_turn_variants():
    assert to_model_turn(blocked_prompt_response()).finish is Finish.BLOCKED
    empty = to_model_turn(gemini_response(finish="SAFETY"))
    assert empty.finish is Finish.BLOCKED and empty.content is None

    calls = to_model_turn(gemini_response(text_part("잠시만요"), call_part("c1", "list_todos", {})))
    assert calls.finish is Finish.TOOL_CALLS
    assert calls.tool_calls == [ToolCall("c1", "list_todos", {})]
    assert calls.content["role"] == "model"

    truncated = to_model_turn(gemini_response(text_part("길"), finish="MAX_TOKENS"))
    assert truncated.finish is Finish.MAX_TOKENS


def test_thought_parts_are_not_user_visible():
    from google.genai import types

    thought = types.Part(text="내부 생각", thought=True)
    turn = to_model_turn(gemini_response(thought, text_part("답변")))
    assert turn.text == "답변"
    model = GeminiModel(FakeGenAI(), "m")
    assert model.render("assistant", turn.content) == ["비서: 답변"]


def test_tool_results_turn_and_pending_calls():
    model = GeminiModel(FakeGenAI(), "m")
    ok, failed = ToolCall("a", "add_todo", {}), ToolCall(None, "forget", {})
    turn = model.tool_results_turn([(ok, ToolResult("추가함")), (failed, ToolResult("없음", is_error=True))])
    assert turn == {
        "role": "user",
        "parts": [
            {"function_response": {"name": "add_todo", "response": {"result": "추가함"}, "id": "a"}},
            {"function_response": {"name": "forget", "response": {"error": "없음"}}},
        ],
    }
    model_turn = {"role": "model", "parts": [{"text": "x"}, {"function_call": {"id": "a", "name": "add_todo", "args": {"t": 1}}}]}
    assert model.pending_tool_calls(model_turn) == [ToolCall("a", "add_todo", {"t": 1})]
    assert model.pending_tool_calls(turn) == []


def test_render_truncates_tool_results():
    model = GeminiModel(FakeGenAI(), "m")
    lines = model.render("user", {"role": "user", "parts": [
        {"text": "추가해 줘"},
        {"function_response": {"name": "x", "response": {"result": "가" * 500}}},
    ]})
    assert lines == ["사용자님: 추가해 줘", "도구 결과: " + "가" * 300]
    call = model.render("assistant", {"role": "model", "parts": [{"function_call": {"name": "add_todo", "args": {"title": "보고서"}}}]})
    assert call == ['비서 도구 호출: add_todo {"title": "보고서"}']


def test_merge_same_role_does_not_mutate_input():
    history = [
        {"role": "user", "parts": [{"text": "a"}]},
        {"role": "user", "parts": [{"text": "b"}]},
        {"role": "model", "parts": [{"text": "c"}]},
    ]
    merged = merge_same_role(history)
    assert merged == [
        {"role": "user", "parts": [{"text": "a"}, {"text": "b"}]},
        {"role": "model", "parts": [{"text": "c"}]},
    ]
    assert history[0]["parts"] == [{"text": "a"}]
