from types import SimpleNamespace

import pytest

from app.channels.telegram_bot import (
    CANCEL,
    CONFIRM,
    FALLBACK_REPLY,
    ChatHandlers,
    ChatServices,
    confirmation_message,
    handle_confirmation,
    parse_callback,
)
from app.tools.todos import todo_tools
from tests.conftest import kst


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("confirm:a-1234", (CONFIRM, "a-1234")),
        ("cancel:a-1234", (CANCEL, "a-1234")),
        ("confirm:", None),
        ("delete:a-1", None),
        (None, None),
    ],
)
def test_parse_callback(data, expected):
    assert parse_callback(data) == expected


@pytest.fixture
def services(registry, conversation, todos, clock):
    registry.register(*todo_tools(todos, clock))
    return ChatServices(assistant=None, registry=registry, conversation=conversation, clock=clock)


async def request_delete(services, todos):
    await todos.add("지울 일", kst(9, 17, 13))
    outcome = await services.registry.call("delete_todo", {"todo_id": 1}, kst(9, 17, 14))
    return outcome.pending


async def test_confirmation_message_has_two_buttons(services, todos):
    action = await request_delete(services, todos)
    message = confirmation_message(action)
    assert message.text == "확인이 필요합니다.\n할 일 삭제 — #1 지울 일"
    assert [(b.label, b.callback_data) for b in message.buttons] == [
        ("확인", f"confirm:{action.id}"),
        ("취소", f"cancel:{action.id}"),
    ]


async def test_confirm_runs_tool_once_and_leaves_note(services, todos, conversation):
    action = await request_delete(services, todos)

    text = await handle_confirmation(services, CONFIRM, action.id)
    assert text == "할 일 삭제 — #1 지울 일\n→ 삭제했습니다: #1 지울 일"
    assert await todos.get(1) is None
    assert await handle_confirmation(services, CONFIRM, action.id) is None
    assert await handle_confirmation(services, CANCEL, action.id) is None
    assert (await conversation.consume_notes())[0].startswith("확인 버튼 처리 결과")


async def test_cancel_keeps_todo(services, todos, conversation):
    action = await request_delete(services, todos)
    assert (await handle_confirmation(services, CANCEL, action.id)).endswith("→ 취소했습니다.")
    assert await todos.get(1) is not None
    assert (await conversation.consume_notes())[0].startswith("취소 버튼 처리")


class FakeBot:
    def __init__(self):
        self.sent = []
        self.actions = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs.get("reply_markup")))

    async def send_chat_action(self, chat_id, action):
        self.actions.append(action)


class FakeQuery:
    def __init__(self, user_id, data):
        self.from_user = SimpleNamespace(id=user_id)
        self.data = data
        self.answers = []
        self.edited = []

    async def answer(self, text=None):
        self.answers.append(text)

    async def edit_message_text(self, text):
        self.edited.append(text)


def context_for(bot, services):
    return SimpleNamespace(bot=bot, bot_data={"services": services})


async def test_callback_from_other_user_is_ignored(services, todos):
    action = await request_delete(services, todos)
    query = FakeQuery(999, f"confirm:{action.id}")
    await ChatHandlers(allowed_user_id=1).on_callback(SimpleNamespace(callback_query=query), context_for(FakeBot(), services))
    assert query.answers == [] and query.edited == []
    assert await todos.get(1) is not None


async def test_callback_from_owner_edits_message(services, todos):
    action = await request_delete(services, todos)
    handlers = ChatHandlers(allowed_user_id=1)
    query = FakeQuery(1, f"confirm:{action.id}")
    await handlers.on_callback(SimpleNamespace(callback_query=query), context_for(FakeBot(), services))
    assert query.edited == ["할 일 삭제 — #1 지울 일\n→ 삭제했습니다: #1 지울 일"]

    again = FakeQuery(1, f"confirm:{action.id}")
    await handlers.on_callback(SimpleNamespace(callback_query=again), context_for(FakeBot(), services))
    assert again.answers == ["이미 처리된 요청입니다."] and again.edited == []


def update_for(text):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=1), effective_message=SimpleNamespace(text=text))


async def test_text_reply_and_confirmation_buttons_are_sent(services, todos):
    action = await request_delete(services, todos)

    class Assistant:
        async def reply(self, text, now):
            from app.agent.loop import AssistantReply

            return AssistantReply("확인 버튼을 보내 드렸습니다.", [action])

    services.assistant = Assistant()
    bot = FakeBot()
    await ChatHandlers(allowed_user_id=1).on_text(update_for("1번 지워"), context_for(bot, services))
    assert [text for _, text, _ in bot.sent] == ["확인 버튼을 보내 드렸습니다.", confirmation_message(action).text]
    assert bot.sent[1][2] is not None
    assert bot.actions == ["typing"]


async def test_assistant_failure_sends_fallback(services):
    class Broken:
        async def reply(self, text, now):
            raise RuntimeError("boom")

    services.assistant = Broken()
    bot = FakeBot()
    await ChatHandlers(allowed_user_id=1).on_text(update_for("안녕"), context_for(bot, services))
    assert [text for _, text, _ in bot.sent] == [FALLBACK_REPLY]


async def test_start_greets_with_configured_honorific(services):
    bot = FakeBot()
    await ChatHandlers(allowed_user_id=1, honorific="길동님").on_start(update_for("/start"), context_for(bot, services))
    assert bot.sent[0][1].startswith("길동님, 비서가 준비되었습니다.")


def test_handlers_only_accept_owner_private_chat():
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler

    application = Application.builder().token("123:abc").build()
    ChatHandlers(allowed_user_id=42).register(application)
    handlers = application.handlers[0]
    assert [type(h) for h in handlers] == [CommandHandler, MessageHandler, CallbackQueryHandler]
    message_filter = handlers[1].filters
    assert "42" in repr(message_filter) and "private" in repr(message_filter).lower()
