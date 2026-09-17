import pytest

from app.channels.telegram import TELEGRAM_MAX_LEN, TelegramNotifier, split_text
from app.core.interfaces import Button, OutgoingMessage


class FakeBot:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append({"chat_id": chat_id, "text": text, **kwargs})


def test_short_text_is_not_split():
    assert split_text("안녕하세요") == ["안녕하세요"]


def test_split_prefers_line_breaks():
    text = "가" * 6 + "\n" + "나" * 6 + "\n" + "다" * 3
    assert split_text(text, limit=10) == ["가" * 6, "나" * 6 + "\n" + "다" * 3]


def test_split_hard_cuts_long_line():
    assert split_text("a" * 25, limit=10) == ["a" * 10, "a" * 10, "a" * 5]


def test_split_keeps_all_chunks_within_limit():
    text = "\n".join(f"· 항목 {i} " + "내용" * 30 for i in range(200))
    chunks = split_text(text)
    assert len(chunks) > 1
    assert all(len(chunk) <= TELEGRAM_MAX_LEN for chunk in chunks)
    assert "\n".join(chunks) == text


def test_empty_text_is_rejected():
    with pytest.raises(ValueError):
        split_text("  \n")


async def test_notifier_sends_plain_text_to_allowed_user_with_buttons_on_last_chunk():
    bot = FakeBot()
    message = OutgoingMessage("가" * 5000, buttons=(Button("되돌리기", "undo:1"),))

    await TelegramNotifier(bot, chat_id=12345).send(message)

    assert len(bot.calls) == 2
    assert {call["chat_id"] for call in bot.calls} == {12345}
    assert all("parse_mode" not in call for call in bot.calls)
    assert bot.calls[0]["reply_markup"] is None
    keyboard = bot.calls[1]["reply_markup"].inline_keyboard
    assert keyboard[0][0].text == "되돌리기"
    assert keyboard[0][0].callback_data == "undo:1"


async def test_notifier_without_buttons_sends_no_markup():
    bot = FakeBot()
    await TelegramNotifier(bot, chat_id=1).send(OutgoingMessage("알림"))
    assert bot.calls == [{"chat_id": 1, "text": "알림", "reply_markup": None}]
