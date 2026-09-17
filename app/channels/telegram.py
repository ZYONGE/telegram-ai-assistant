"""텔레그램 출력. 사용자 한 명(허용된 ID)에게만 보낸다."""

from typing import Any, Protocol

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.interfaces import Button, OutgoingMessage

TELEGRAM_MAX_LEN = 4096


class _MessageSender(Protocol):
    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any: ...


class TelegramNotifier:
    def __init__(self, bot: _MessageSender, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id

    async def send(self, message: OutgoingMessage) -> None:
        chunks = split_text(message.text)
        markup = _keyboard(message.buttons)
        for index, chunk in enumerate(chunks):
            is_last = index == len(chunks) - 1
            # parse_mode를 지정하지 않아 마크다운이 해석되지 않는다
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=chunk,
                reply_markup=markup if is_last else None,
            )


def split_text(text: str, limit: int = TELEGRAM_MAX_LEN) -> list[str]:
    """줄바꿈 위치를 우선해 limit 이하 조각으로 나눈다."""
    if not text.strip():
        raise ValueError("빈 메시지는 보낼 수 없습니다")
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit + 1)
        if cut <= 0:
            chunks.append(rest[:limit])
            rest = rest[limit:]
        else:
            chunks.append(rest[:cut])
            rest = rest[cut + 1 :]
    if rest:
        chunks.append(rest)
    return chunks


def _keyboard(buttons: tuple[Button, ...]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(button.label, callback_data=button.callback_data)] for button in buttons]
    )
