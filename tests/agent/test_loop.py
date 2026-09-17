from datetime import timedelta

import anthropic
import httpx2
import pytest

from app.agent.light import LightModel, render_transcript
from app.agent.loop import REFUSAL_REPLY, TOO_MANY_STEPS_REPLY, Assistant, strip_markdown
from app.agent.memory import MarkdownMemoryStore
from app.agent.prompt import PromptBuilder
from app.core.config import ConversationSettings
from app.tools.todos import todo_tools
from tests.conftest import FakeAnthropic, kst, response, text, thinking, tool_use


@pytest.fixture
def prompt(tmp_path):
    template = tmp_path / "system.md"
    template.write_text("비서입니다.\n{profile}\n{memory}\n요약: {summary}", encoding="utf-8")
    return PromptBuilder(template, tmp_path / "profile.md", MarkdownMemoryStore(tmp_path / "memory.md"))


@pytest.fixture
def make_assistant(prompt, conversation, registry, todos, clock):
    registry.register(*todo_tools(todos, clock))

    def build(*responses, light_responses=(), settings=None):
        client = FakeAnthropic(*responses)
        light_client = FakeAnthropic(*light_responses)
        assistant = Assistant(
            client,
            "chat-model",
            settings or ConversationSettings(),
            prompt,
            conversation,
            registry,
            LightModel(light_client, "light-model"),
        )
        return assistant, client, light_client

    return build


async def test_plain_reply_is_saved_with_time_prefix(make_assistant, conversation):
    assistant, client, _ = make_assistant(response(thinking(), text("**네**, 사용자님.")))
    reply = await assistant.reply("안녕", kst(9, 17, 14))

    assert reply.text == "네, 사용자님."
    call = client.messages.calls[0]
    assert call["model"] == "chat-model"
    assert call["cache_control"] == {"type": "ephemeral"}
    assert call["system"].startswith("비서입니다.")
    assert [d["name"] for d in call["tools"]] == sorted(d["name"] for d in call["tools"])
    user_blocks = call["messages"][0]["content"]
    assert user_blocks[0]["text"] == "[현재 시각] 2026-09-17(목) 14:00 (Asia/Seoul)"
    assert user_blocks[-1]["text"] == "안녕"

    stored = await conversation.active_messages(kst(9, 17, 14))
    assert [m.role for m in stored] == ["user", "assistant"]
    assert stored[1].content[0] == {"type": "thinking", "thinking": "", "signature": "sig"}


async def test_tool_round_trip_is_persisted_and_replayed(make_assistant, conversation, todos):
    assistant, client, _ = make_assistant(
        response(tool_use("tu1", "add_todo", {"title": "보고서", "due": "2026-09-20"}), stop="tool_use"),
        response(text("할 일에 추가해 두었습니다.")),
        response(text("보고서가 하나 있습니다.")),
    )
    first = await assistant.reply("보고서 20일까지 추가해 줘", kst(9, 17, 14))
    assert first.text == "할 일에 추가해 두었습니다."
    assert [t.title for t in await todos.list_open()] == ["보고서"]

    second_call = client.messages.calls[1]["messages"]
    assert [m["role"] for m in second_call] == ["user", "assistant", "user"]
    result = second_call[2]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "tu1"
    assert "보고서" in result["content"] and "is_error" not in result

    await assistant.reply("할 일 뭐 있어?", kst(9, 17, 14, 5))
    third_call = client.messages.calls[2]["messages"]
    assert [m["role"] for m in third_call] == ["user", "assistant", "user", "assistant", "user"]
    assert len(await conversation.active_messages(kst(9, 17, 15))) == 6


async def test_tool_error_is_returned_to_model(make_assistant):
    assistant, client, _ = make_assistant(
        response(tool_use("tu1", "complete_todo", {"todo_id": 42}), stop="tool_use"),
        response(text("그 번호의 할 일이 없습니다.")),
    )
    await assistant.reply("42번 완료", kst(9, 17, 14))
    result = client.messages.calls[1]["messages"][2]["content"][0]
    assert result["is_error"] is True


async def test_button_tool_returns_confirmation(make_assistant, todos):
    await todos.add("지울 일", kst(9, 17, 13))
    assistant, _, _ = make_assistant(
        response(text("확인 버튼을 보내 드렸습니다."), tool_use("tu1", "delete_todo", {"todo_id": 1}), stop="tool_use"),
        response(text("확인을 누르시면 삭제됩니다.")),
    )
    reply = await assistant.reply("1번 지워 줘", kst(9, 17, 14))
    assert reply.text == "확인을 누르시면 삭제됩니다."
    assert [a.tool_name for a in reply.confirmations] == ["delete_todo"]
    assert await todos.get(1) is not None


async def test_refusal_and_truncation(make_assistant):
    assistant, client, _ = make_assistant(response(stop="refusal"), response(text("길게"), stop="max_tokens"))
    assert (await assistant.reply("a", kst(9, 17, 14))).text == REFUSAL_REPLY
    assert (await assistant.reply("b", kst(9, 17, 14))).text == "길게\n(답변이 길어 중간에 끊겼습니다.)"
    # 빈 거부 응답은 기록되지 않아 다음 요청에 빈 assistant 메시지가 없다
    assert [m["role"] for m in client.messages.calls[1]["messages"]] == ["user", "user"]


async def test_tool_loop_is_bounded(make_assistant):
    looping = [response(tool_use(f"tu{i}", "list_todos", {}), stop="tool_use") for i in range(8)]
    assistant, client, _ = make_assistant(*looping)
    reply = await assistant.reply("계속", kst(9, 17, 14))
    assert reply.text == TOO_MANY_STEPS_REPLY
    assert len(client.messages.calls) == 8


async def test_notes_are_attached_to_next_message(make_assistant, conversation):
    await conversation.add_note("확인 버튼 처리 결과 — 할 일 삭제 → 삭제했습니다", kst(9, 17, 13))
    assistant, client, _ = make_assistant(response(text("네")), response(text("네")))
    await assistant.reply("고마워", kst(9, 17, 14))
    blocks = client.messages.calls[0]["messages"][0]["content"]
    assert blocks[1]["text"].startswith("[알림] 확인 버튼 처리 결과")
    await assistant.reply("또", kst(9, 17, 14))
    assert all("[알림]" not in b["text"] for b in client.messages.calls[1]["messages"][-1]["content"])


async def test_api_error_propagates_and_history_stays_valid(make_assistant, conversation):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    assistant, client, _ = make_assistant(anthropic.APIConnectionError(request=request), response(text("복구")))
    with pytest.raises(anthropic.APIConnectionError):
        await assistant.reply("첫 메시지", kst(9, 17, 14))
    assert (await assistant.reply("다시", kst(9, 17, 14, 1))).text == "복구"
    roles = [m["role"] for m in client.messages.calls[1]["messages"]]
    assert roles == ["user", "user"]


async def test_idle_conversation_is_compacted(make_assistant, conversation):
    assistant, client, light = make_assistant(
        response(text("네, 기억하겠습니다.")),
        response(text("이어서 말씀드리면")),
        light_responses=[response(text("- 보고서 마감을 이야기함"))],
    )
    await assistant.reply("보고서 얘기 좀 하자", kst(9, 17, 14))

    assert await assistant.compact_if_idle(kst(9, 17, 14, 10)) is False
    assert await assistant.compact_if_idle(kst(9, 17, 14, 31)) is True
    assert await conversation.summary() == "- 보고서 마감을 이야기함"
    assert "보고서 얘기 좀 하자" in light.messages.calls[0]["messages"][0]["content"]
    assert light.messages.calls[0]["model"] == "light-model"

    await assistant.reply("이어서", kst(9, 17, 15))
    call = client.messages.calls[1]
    assert len(call["messages"]) == 1
    assert "요약: - 보고서 마감을 이야기함" in call["system"]


async def test_long_conversation_is_compacted_before_next_message(make_assistant, conversation):
    replies = [response(text(f"답{i}")) for i in range(3)]
    assistant, client, light = make_assistant(
        *replies, light_responses=[response(text("- 요약"))], settings=ConversationSettings(max_active_messages=4)
    )
    for i in range(3):
        await assistant.reply(f"질문{i}", kst(9, 17, 14, i))
    assert len(client.messages.calls[2]["messages"]) == 1
    assert len(light.messages.calls) == 1


async def test_failed_summary_keeps_history(make_assistant, conversation):
    assistant, _, _ = make_assistant(
        response(text("네")), light_responses=[response(text(""), stop="max_tokens")]
    )
    await assistant.reply("안녕", kst(9, 17, 14))
    assert await assistant.compact_if_idle(kst(9, 17, 15)) is False
    assert len(await conversation.active_messages(kst(9, 17, 15))) == 2


async def test_run_task_is_separate_from_chat_and_has_no_button_tools(make_assistant, conversation):
    assistant, client, _ = make_assistant(response(text("오늘은 보고서부터 하세요.")))
    result = await assistant.run_task("우선순위 정리", kst(9, 17, 7))
    assert result == "오늘은 보고서부터 하세요."
    call = client.messages.calls[0]
    assert "delete_todo" not in [d["name"] for d in call["tools"]]
    assert "[예약 작업]" in call["messages"][0]["content"][1]["text"]
    assert await conversation.active_messages(kst(9, 17, 7)) == []


async def test_polish_falls_back_to_draft_on_error():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    light = LightModel(FakeAnthropic(anthropic.APIConnectionError(request=request)), "light")
    from app.core.interfaces import BriefingKind

    assert await light.polish_briefing(BriefingKind.MORNING, "초안") == "초안"
    ok = LightModel(FakeAnthropic(response(text("다듬은 문장"))), "light")
    assert await ok.polish_briefing(BriefingKind.MORNING, "초안") == "다듬은 문장"


def test_render_transcript_includes_tools_and_truncates_results():
    from app.storage.conversation import StoredMessage

    messages = [
        StoredMessage(1, "user", [{"type": "text", "text": "추가해 줘"}], kst(9, 17, 14)),
        StoredMessage(2, "assistant", [
            {"type": "thinking", "thinking": "", "signature": "s"},
            {"type": "tool_use", "id": "t", "name": "add_todo", "input": {"title": "보고서"}},
        ], kst(9, 17, 14)),
        StoredMessage(3, "user", [{"type": "tool_result", "tool_use_id": "t", "content": "가" * 500}], kst(9, 17, 14)),
    ]
    lines = render_transcript(messages).splitlines()
    assert lines[0] == "사용자님: 추가해 줘"
    assert lines[1] == '비서 도구 호출: add_todo {"title": "보고서"}'
    assert lines[2] == "도구 결과: " + "가" * 300


def test_strip_markdown():
    assert strip_markdown("## 오늘\n- **보고서**\n* `코드`") == "오늘\n· 보고서\n· 코드"
