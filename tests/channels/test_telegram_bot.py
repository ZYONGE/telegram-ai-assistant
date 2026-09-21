from types import SimpleNamespace

import pytest

from app.channels.telegram_bot import (
    CANCEL,
    CONFIRM,
    FALLBACK_REPLY,
    LOCATION_HIDDEN,
    LOCATION_LIVE,
    UNDO_NOTHING,
    LOCATION_OFF,
    LOCATION_SAVED,
    ChatHandlers,
    ChatServices,
    confirmation_message,
    handle_confirmation,
    parse_callback,
)
from app.storage.location import LocationStore
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
    assert message.text == "할 일 삭제 — #1 지울 일"
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
        self.cleared = False
        self.message = SimpleNamespace(chat_id=1)

    async def answer(self, text=None):
        self.answers.append(text)

    async def edit_message_text(self, text):
        self.edited.append(text)

    async def edit_message_reply_markup(self, reply_markup=None):
        self.cleared = True


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
    bot = FakeBot()
    await handlers.on_callback(SimpleNamespace(callback_query=query), context_for(bot, services))
    # 버튼만 치우고 비서가 한 말은 그대로 둔다. 결과는 새 메시지로 (목소리가 없으면 초안 그대로)
    assert query.cleared and query.edited == []
    assert [text for _, text, _ in bot.sent] == ["할 일 삭제 — #1 지울 일\n→ 삭제했습니다: #1 지울 일"]

    again = FakeQuery(1, f"confirm:{action.id}")
    await handlers.on_callback(SimpleNamespace(callback_query=again), context_for(FakeBot(), services))
    assert again.answers == ["이미 처리된 요청입니다."] and not again.cleared


def update_for(text):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=1), effective_message=SimpleNamespace(text=text))


async def test_text_reply_and_confirmation_buttons_are_sent(services, todos):
    action = await request_delete(services, todos)

    class Assistant:
        async def reply(self, text, now, progress=None):
            from app.agent.loop import AssistantReply

            return AssistantReply("확인 버튼을 보내 드렸습니다.", [action])

    services.assistant = Assistant()
    bot = FakeBot()
    await ChatHandlers(allowed_user_id=1).on_text(update_for("1번 지워"), context_for(bot, services))
    # 확인할 것이 하나면 비서의 말 아래에 버튼을 바로 단다. 정해 둔 문구를 한 번 더 보내지 않는다.
    assert [text for _, text, _ in bot.sent] == ["확인 버튼을 보내 드렸습니다."]
    assert bot.sent[0][2] is not None
    assert bot.actions == ["typing"]


async def test_several_confirmations_are_told_apart(services, todos):
    first = await request_delete(services, todos)
    await todos.add("또 지울 일", kst(9, 17, 13))
    second = (await services.registry.call("delete_todo", {"todo_id": 2}, kst(9, 17, 14))).pending

    class Assistant:
        async def reply(self, text, now, progress=None):
            from app.agent.loop import AssistantReply

            return AssistantReply("두 개 지울까요?", [first, second])

    services.assistant = Assistant()
    bot = FakeBot()
    await ChatHandlers(allowed_user_id=1).on_text(update_for("둘 다 지워"), context_for(bot, services))
    assert [text for _, text, _ in bot.sent] == ["두 개 지울까요?", first.summary, second.summary]
    assert bot.sent[0][2] is None and bot.sent[1][2] is not None and bot.sent[2][2] is not None


class FakeVoice:
    def __init__(self):
        self.calls = []

    async def say(self, draft, situation):
        self.calls.append((draft, situation))
        return f"[다듬음] {draft.splitlines()[0]}"


async def test_button_results_are_said_by_the_assistant_not_a_template(services, todos):
    services.voice = FakeVoice()
    action = await request_delete(services, todos)
    bot = FakeBot()
    await ChatHandlers(allowed_user_id=1).on_callback(
        SimpleNamespace(callback_query=FakeQuery(1, f"confirm:{action.id}")), context_for(bot, services)
    )
    assert bot.sent[0][1] == "[다듬음] 할 일 삭제 — #1 지울 일"
    draft, situation = services.voice.calls[0]
    assert "삭제했습니다" in draft and "버튼" in situation


async def test_a_failing_voice_falls_back_to_the_draft(services, todos):
    class Broken:
        async def say(self, draft, situation):
            raise RuntimeError("모델 장애")

    services.voice = Broken()
    action = await request_delete(services, todos)
    bot = FakeBot()
    await ChatHandlers(allowed_user_id=1).on_callback(
        SimpleNamespace(callback_query=FakeQuery(1, f"cancel:{action.id}")), context_for(bot, services)
    )
    assert bot.sent[0][1].endswith("→ 취소했습니다.")


async def test_assistant_failure_sends_fallback(services):
    class Broken:
        async def reply(self, text, now, progress=None):
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
    assert [type(h) for h in handlers] == [
        CommandHandler,  # /start
        CommandHandler,  # /location
        MessageHandler,  # 일반 대화
        MessageHandler,  # 위치 메시지와 실시간 공유 갱신
        CallbackQueryHandler,
    ]
    message_filter = handlers[2].filters
    assert "42" in repr(message_filter) and "private" in repr(message_filter).lower()
    location_filter = repr(handlers[3].filters)
    assert "LOCATION" in location_filter.upper() and "42" in location_filter


def location_update(latitude=37.5665, longitude=126.9780, live_period=None, edited=False):
    location = SimpleNamespace(latitude=latitude, longitude=longitude, live_period=live_period)
    message = SimpleNamespace(location=location)
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=1),
        effective_message=message,
        edited_message=message if edited else None,
    )


@pytest.fixture
def located(services, db):
    services.location = LocationStore(db)
    return services


async def test_location_is_saved_and_confirmed_once(located):
    bot = FakeBot()
    await ChatHandlers(allowed_user_id=1).on_location(location_update(), context_for(bot, located))

    stored = await located.location.latest()
    assert (round(stored.lat, 4), round(stored.lon, 4)) == (37.5665, 126.978)
    assert stored.live_until is None
    assert [text for _, text, _ in bot.sent] == [LOCATION_SAVED]


async def test_live_location_sets_end_time_and_says_so(located, clock):
    located.clock = clock
    bot = FakeBot()
    await ChatHandlers(allowed_user_id=1).on_location(
        location_update(live_period=3600), context_for(bot, located)
    )

    stored = await located.location.latest()
    assert (stored.live_until - clock.now).total_seconds() == 3600
    assert [text for _, text, _ in bot.sent] == [LOCATION_LIVE]


async def test_live_updates_are_saved_without_replying(located):
    bot = FakeBot()
    handlers = ChatHandlers(allowed_user_id=1)
    await handlers.on_location(location_update(live_period=3600), context_for(bot, located))
    await handlers.on_location(
        location_update(latitude=35.1796, longitude=129.0756, live_period=3600, edited=True),
        context_for(bot, located),
    )

    stored = await located.location.latest()
    assert round(stored.lat, 4) == 35.1796
    assert len(bot.sent) == 1


async def test_location_without_store_is_reported(services):
    services.location = None
    bot = FakeBot()
    await ChatHandlers(allowed_user_id=1).on_location(location_update(), context_for(bot, services))
    assert [text for _, text, _ in bot.sent] == [LOCATION_OFF]


async def test_coordinates_never_appear_in_logs(located, caplog):
    import logging

    with caplog.at_level(logging.INFO):
        await ChatHandlers(allowed_user_id=1).on_location(location_update(), context_for(FakeBot(), located))
    assert "37.5665" not in caplog.text and "126.978" not in caplog.text


def command_update(text="/location"):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=1),
        effective_message=SimpleNamespace(text=text),
    )


async def test_location_command_shows_the_button(located):
    bot = FakeBot()
    await ChatHandlers(allowed_user_id=1).on_location_command(command_update(), context_for(bot, located))
    text, markup = bot.sent[0][1], bot.sent[0][2]
    assert text.startswith("아래 [위치 보내기] 버튼")
    assert markup.keyboard[0][0].request_location is True
    assert markup.is_persistent is True


async def test_location_command_can_hide_the_button(located):
    bot = FakeBot()
    await ChatHandlers(allowed_user_id=1).on_location_command(
        command_update("/location 끄기"), context_for(bot, located)
    )
    assert bot.sent[0][1] == LOCATION_HIDDEN
    assert bot.sent[0][2].__class__.__name__ == "ReplyKeyboardRemove"


async def test_start_offers_the_button_until_a_location_is_known(located):
    bot = FakeBot()
    handlers = ChatHandlers(allowed_user_id=1, honorific="길동님")
    await handlers.on_start(update_for("/start"), context_for(bot, located))
    assert "위치 보내기" in bot.sent[0][1] and bot.sent[0][2] is not None

    await located.location.save(37.5665, 126.9780, kst(9, 18, 11))
    await handlers.on_start(update_for("/start"), context_for(bot, located))
    assert "위치 보내기" not in bot.sent[1][1] and bot.sent[1][2] is None


async def test_saved_location_reply_keeps_the_button(located):
    bot = FakeBot()
    await ChatHandlers(allowed_user_id=1).on_location(location_update(), context_for(bot, located))
    assert bot.sent[0][2].keyboard[0][0].request_location is True


class FakeMailService:
    def __init__(self, restored=2, failed=0) -> None:
        self.result = (restored, failed)
        self.calls: list = []

    async def undo_cleanup(self, day_start):
        self.calls.append(day_start)
        return self.result


def callback_update(data: str, user_id: int = 1):
    return SimpleNamespace(callback_query=FakeQuery(user_id, data))


async def test_undo_button_restores_mail_and_reports(services):
    mail = FakeMailService(restored=3, failed=1)
    services.mail = mail
    query = FakeQuery(1, "undo:mail:20260918")
    bot = FakeBot()
    await ChatHandlers(allowed_user_id=1).on_callback(
        SimpleNamespace(callback_query=query), context_for(bot, services)
    )

    assert [call.strftime("%Y-%m-%d") for call in mail.calls] == ["2026-09-18"]
    assert query.cleared
    assert bot.sent[0][1].startswith("되돌렸습니다: 메일 3건")
    assert "1건은 되돌리지 못했습니다" in bot.sent[0][1]
    assert (await services.conversation.consume_notes())[0].startswith("메일 정리 되돌리기")


async def test_undo_without_mail_service_says_nothing_to_undo(services):
    services.mail = None
    query = FakeQuery(1, "undo:mail:20260918")
    bot = FakeBot()
    await ChatHandlers(allowed_user_id=1).on_callback(
        SimpleNamespace(callback_query=query), context_for(bot, services)
    )
    assert [text for _, text, _ in bot.sent] == [UNDO_NOTHING]


async def test_undo_from_another_user_is_ignored(services):
    services.mail = FakeMailService()
    query = FakeQuery(999, "undo:mail:20260918")
    await ChatHandlers(allowed_user_id=1).on_callback(
        SimpleNamespace(callback_query=query), context_for(FakeBot(), services)
    )
    assert query.edited == [] and not query.cleared and services.mail.calls == []
