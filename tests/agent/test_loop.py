import pytest

from app.agent.light import LightModel
from app.agent.loop import BLOCKED_REPLY, INTERRUPTED_RESULT, TOO_MANY_STEPS_REPLY, Assistant, strip_markdown
from app.agent.memory import MarkdownMemoryStore
from app.agent.prompt import PromptBuilder
from app.core.config import ConversationSettings
from app.core.interfaces import BriefingKind
from app.llm import TransientLLMError
from app.llm.gemini import GeminiModel
from app.tools.todos import todo_tools
from tests.conftest import (
    FakeGenAI,
    blocked_prompt_response,
    call_part,
    gemini_response,
    kst,
    server_error,
    text_part,
)


@pytest.fixture
def prompt(tmp_path):
    template = tmp_path / "system.md"
    template.write_text("비서입니다.\n{profile}\n{memory}\n요약: {summary}", encoding="utf-8")
    return PromptBuilder(template, tmp_path / "profile.md", MarkdownMemoryStore(tmp_path / "memory.md"))


@pytest.fixture
def make_assistant(prompt, conversation, registry, todos, clock):
    registry.register(*todo_tools(todos, clock))

    def build(*responses, light_responses=(), settings=None):
        chat = FakeGenAI(*responses)
        light = FakeGenAI(*light_responses)
        assistant = Assistant(
            GeminiModel(chat, "chat-model"),
            settings or ConversationSettings(),
            prompt,
            conversation,
            registry,
            LightModel(GeminiModel(light, "light-model")),
        )
        return assistant, chat.models, light.models

    return build


def roles(call):
    return [turn["role"] for turn in call["contents"]]


async def test_plain_reply_uses_config_and_time_prefix(make_assistant, conversation):
    assistant, chat, _ = make_assistant(gemini_response(text_part("**네**, 사용자님.", signature=b"sig")))
    reply = await assistant.reply("안녕", kst(9, 17, 14))

    assert reply.text == "네, 사용자님."
    call = chat.calls[0]
    assert call["model"] == "chat-model"
    config = call["config"]
    assert config.system_instruction.startswith("비서입니다.")
    assert config.automatic_function_calling.disable is True
    names = [d.name for d in config.tools[0].function_declarations]
    assert names == sorted(names) and "add_todo" in names
    user_parts = call["contents"][0]["parts"]
    assert user_parts[0]["text"] == "[현재 시각] 2026-09-17(목) 14:00 (Asia/Seoul)"
    assert user_parts[-1]["text"] == "안녕"

    stored = await conversation.active_messages()
    assert [m.role for m in stored] == ["user", "assistant"]
    assert stored[1].content["role"] == "model"
    assert stored[1].content["parts"][0]["thought_signature"] == "c2ln"


async def test_tool_round_trip_keeps_thought_signature(make_assistant, conversation, todos):
    assistant, chat, _ = make_assistant(
        gemini_response(call_part("c1", "add_todo", {"title": "보고서", "due": "2026-09-20"}, signature=b"\x01sig")),
        gemini_response(text_part("할 일에 추가해 두었습니다.")),
        gemini_response(text_part("보고서가 하나 있습니다.")),
    )
    first = await assistant.reply("보고서 20일까지 추가해 줘", kst(9, 17, 14))
    assert first.text == "할 일에 추가해 두었습니다."
    assert [t.title for t in await todos.list_open()] == ["보고서"]

    second = chat.calls[1]
    assert roles(second) == ["user", "model", "user"]
    assert second["contents"][1]["parts"][0]["thought_signature"] == "AXNpZw=="
    response_part = second["contents"][2]["parts"][0]["function_response"]
    assert response_part["id"] == "c1" and response_part["name"] == "add_todo"
    assert "보고서" in response_part["response"]["result"]

    await assistant.reply("할 일 뭐 있어?", kst(9, 17, 14, 5))
    assert roles(chat.calls[2]) == ["user", "model", "user", "model", "user"]
    assert len(await conversation.active_messages()) == 6


async def test_tool_error_is_returned_as_error_response(make_assistant):
    assistant, chat, _ = make_assistant(
        gemini_response(call_part("c1", "complete_todo", {"todo_id": 42})),
        gemini_response(text_part("그 번호의 할 일이 없습니다.")),
    )
    await assistant.reply("42번 완료", kst(9, 17, 14))
    response_part = chat.calls[1]["contents"][2]["parts"][0]["function_response"]["response"]
    assert "error" in response_part and "result" not in response_part


async def test_parallel_calls_are_answered_in_one_turn(make_assistant, todos):
    assistant, chat, _ = make_assistant(
        gemini_response(call_part("a", "add_todo", {"title": "A"}), call_part("b", "add_todo", {"title": "B"})),
        gemini_response(text_part("두 개 추가했습니다.")),
    )
    await assistant.reply("A, B 추가", kst(9, 17, 14))
    parts = chat.calls[1]["contents"][2]["parts"]
    assert [p["function_response"]["id"] for p in parts] == ["a", "b"]
    assert len(await todos.list_open()) == 2


async def test_button_tool_returns_confirmation(make_assistant, todos):
    await todos.add("지울 일", kst(9, 17, 13))
    assistant, _, _ = make_assistant(
        gemini_response(text_part("확인 버튼을 보내 드리겠습니다."), call_part("c1", "delete_todo", {"todo_id": 1})),
        gemini_response(text_part("확인을 누르시면 삭제됩니다.")),
    )
    reply = await assistant.reply("1번 지워 줘", kst(9, 17, 14))
    assert reply.text == "확인을 누르시면 삭제됩니다."
    assert [a.tool_name for a in reply.confirmations] == ["delete_todo"]
    assert await todos.get(1) is not None


async def test_blocked_and_truncated_replies(make_assistant):
    assistant, chat, _ = make_assistant(
        blocked_prompt_response(),
        gemini_response(finish="SAFETY"),
        gemini_response(text_part("길게"), finish="MAX_TOKENS"),
    )
    assert (await assistant.reply("a", kst(9, 17, 14))).text == BLOCKED_REPLY
    assert (await assistant.reply("b", kst(9, 17, 14))).text == BLOCKED_REPLY
    assert (await assistant.reply("c", kst(9, 17, 14))).text == "길게\n(답변이 길어 중간에 끊겼습니다.)"
    # 빈 응답은 기록되지 않고, 이어진 사용자 턴은 하나로 합쳐져 전송된다
    assert roles(chat.calls[2]) == ["user"]
    assert [p["text"] for p in chat.calls[2]["contents"][0]["parts"] if not p["text"].startswith("[")] == ["a", "b", "c"]


async def test_tool_loop_is_bounded(make_assistant):
    looping = [gemini_response(call_part(f"c{i}", "list_todos", {})) for i in range(8)]
    assistant, chat, _ = make_assistant(*looping)
    reply = await assistant.reply("계속", kst(9, 17, 14))
    assert reply.text == TOO_MANY_STEPS_REPLY
    assert len(chat.calls) == 8


async def test_interrupted_tool_call_is_closed_before_next_message(make_assistant, conversation):
    await conversation.append("user", {"role": "user", "parts": [{"text": "추가"}]}, kst(9, 17, 13))
    await conversation.append(
        "assistant",
        {"role": "model", "parts": [{"function_call": {"id": "c9", "name": "add_todo", "args": {}}}]},
        kst(9, 17, 13),
    )
    assistant, chat, _ = make_assistant(gemini_response(text_part("다시 말씀해 주세요.")))
    await assistant.reply("아까 거 어떻게 됐어?", kst(9, 17, 14))

    contents = chat.calls[0]["contents"]
    assert roles(chat.calls[0]) == ["user", "model", "user"]
    closing = contents[2]["parts"][0]["function_response"]
    assert closing["id"] == "c9" and closing["response"] == {"error": INTERRUPTED_RESULT}
    assert contents[2]["parts"][-1]["text"] == "아까 거 어떻게 됐어?"


async def test_notes_are_attached_to_next_message(make_assistant, conversation):
    await conversation.add_note("확인 버튼 처리 결과 — 할 일 삭제 → 삭제했습니다", kst(9, 17, 13))
    assistant, chat, _ = make_assistant(gemini_response(text_part("네")), gemini_response(text_part("네")))
    await assistant.reply("고마워", kst(9, 17, 14))
    assert chat.calls[0]["contents"][0]["parts"][1]["text"].startswith("[알림] 확인 버튼 처리 결과")
    await assistant.reply("또", kst(9, 17, 14))
    assert all("[알림]" not in p["text"] for p in chat.calls[1]["contents"][-1]["parts"])


async def test_api_error_propagates_and_history_stays_valid(make_assistant):
    assistant, chat, _ = make_assistant(server_error(), gemini_response(text_part("복구")))
    with pytest.raises(TransientLLMError):
        await assistant.reply("첫 메시지", kst(9, 17, 14))
    assert (await assistant.reply("다시", kst(9, 17, 14, 1))).text == "복구"
    assert roles(chat.calls[1]) == ["user"]


async def test_idle_conversation_is_compacted(make_assistant, conversation):
    assistant, chat, light = make_assistant(
        gemini_response(text_part("네, 기억하겠습니다.")),
        gemini_response(text_part("이어서 말씀드리면")),
        light_responses=[gemini_response(text_part("- 보고서 마감을 이야기함"))],
    )
    await assistant.reply("보고서 얘기 좀 하자", kst(9, 17, 14))

    assert await assistant.compact_if_idle(kst(9, 17, 14, 10)) is False
    assert await assistant.compact_if_idle(kst(9, 17, 14, 31)) is True
    assert await conversation.summary() == "- 보고서 마감을 이야기함"
    light_call = light.calls[0]
    assert light_call["model"] == "light-model"
    assert "사용자님: 보고서 얘기 좀 하자" in light_call["contents"][0]["parts"][0]["text"]
    assert light_call["config"].tools is None

    await assistant.reply("이어서", kst(9, 17, 15))
    call = chat.calls[1]
    assert len(call["contents"]) == 1
    assert "요약: - 보고서 마감을 이야기함" in call["config"].system_instruction


async def test_long_conversation_is_compacted_before_next_message(make_assistant):
    replies = [gemini_response(text_part(f"답{i}")) for i in range(3)]
    assistant, chat, light = make_assistant(
        *replies,
        light_responses=[gemini_response(text_part("- 요약"))],
        settings=ConversationSettings(max_active_messages=4),
    )
    for i in range(3):
        await assistant.reply(f"질문{i}", kst(9, 17, 14, i))
    assert len(chat.calls[2]["contents"]) == 1
    assert len(light.calls) == 1


async def test_failed_summary_keeps_history(make_assistant, conversation):
    assistant, _, _ = make_assistant(
        gemini_response(text_part("네")), light_responses=[gemini_response(text_part("잘"), finish="MAX_TOKENS")]
    )
    await assistant.reply("안녕", kst(9, 17, 14))
    assert await assistant.compact_if_idle(kst(9, 17, 15)) is False
    assert len(await conversation.active_messages()) == 2


async def test_run_task_is_separate_from_chat_and_has_no_button_tools(make_assistant, conversation):
    assistant, chat, _ = make_assistant(gemini_response(text_part("오늘은 보고서부터 하세요.")))
    result = await assistant.run_task("우선순위 정리", kst(9, 17, 7))
    assert result == "오늘은 보고서부터 하세요."
    call = chat.calls[0]
    assert "delete_todo" not in [d.name for d in call["config"].tools[0].function_declarations]
    assert "[예약 작업]" in call["contents"][0]["parts"][1]["text"]
    assert await conversation.active_messages() == []


async def test_polish_falls_back_to_draft():
    failing = LightModel(GeminiModel(FakeGenAI(server_error()), "light"))
    assert await failing.polish_briefing(BriefingKind.MORNING, "초안") == "초안"
    blocked = LightModel(GeminiModel(FakeGenAI(gemini_response(finish="SAFETY")), "light"))
    assert await blocked.polish_briefing(BriefingKind.MORNING, "초안") == "초안"
    ok = LightModel(GeminiModel(FakeGenAI(gemini_response(text_part("다듬은 문장"))), "light"))
    assert await ok.polish_briefing(BriefingKind.MORNING, "초안") == "다듬은 문장"


def test_strip_markdown():
    assert strip_markdown("## 오늘\n- **보고서**\n* `코드`") == "오늘\n· 보고서\n· 코드"
