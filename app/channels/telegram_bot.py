"""텔레그램 수신: 허용된 사용자 한 명의 메시지와 확인 버튼만 처리한다. 그 외는 응답하지 않는다."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import anthropic
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from app.agent.loop import Assistant
from app.channels.telegram import TelegramNotifier
from app.core.clock import utc_now
from app.core.interfaces import Button, OutgoingMessage
from app.storage.conversation import ConversationStore, PendingAction
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

SERVICES_KEY = "services"
CONFIRM, CANCEL = "confirm", "cancel"
FALLBACK_REPLY = "지금은 답변을 만들 수 없습니다. 잠시 후 다시 말씀해 주세요."
ALREADY_HANDLED = "이미 처리된 요청입니다."
GREETING = "사용자님, 비서가 준비되었습니다. 할 일이나 리마인더를 편하게 말씀해 주세요."
# 일시적인 API 장애는 스택 추적 없이 한 줄만 남긴다
_TRANSIENT_ERRORS = (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError)


@dataclass(slots=True)
class ChatServices:
    assistant: Assistant
    registry: ToolRegistry
    conversation: ConversationStore
    clock: Callable[[], datetime] = utc_now


def confirmation_message(action: PendingAction) -> OutgoingMessage:
    return OutgoingMessage(
        f"확인이 필요합니다.\n{action.summary}",
        buttons=(Button("확인", f"{CONFIRM}:{action.id}"), Button("취소", f"{CANCEL}:{action.id}")),
    )


def parse_callback(data: str | None) -> tuple[str, str] | None:
    verb, _, action_id = (data or "").partition(":")
    if verb in (CONFIRM, CANCEL) and action_id:
        return verb, action_id
    return None


async def handle_confirmation(services: ChatServices, verb: str, action_id: str) -> str | None:
    """버튼 처리 결과 문구. 이미 처리된 요청이면 None."""
    now = services.clock()
    if verb == CONFIRM:
        outcome = await services.registry.confirm(action_id, now)
        if outcome is None:
            return None
        action, result = outcome
        text = f"{action.summary}\n→ {result.content}"
        note = f"확인 버튼 처리 결과 — {action.summary} → {result.content}"
    else:
        action = await services.registry.cancel(action_id, now)
        if action is None:
            return None
        text = f"{action.summary}\n→ 취소했습니다."
        note = f"취소 버튼 처리 — {action.summary}"
    # 모델이 다음 대화에서 결과를 알 수 있게 남긴다
    await services.conversation.add_note(note, now)
    return text


class ChatHandlers:
    def __init__(self, allowed_user_id: int) -> None:
        self._allowed_user_id = allowed_user_id

    def register(self, application: Application) -> None:
        only_owner = filters.User(user_id=self._allowed_user_id) & filters.ChatType.PRIVATE
        application.add_handler(CommandHandler("start", self.on_start, filters=only_owner))
        application.add_handler(MessageHandler(only_owner & filters.TEXT & ~filters.COMMAND, self.on_text))
        application.add_handler(CallbackQueryHandler(self.on_callback))

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await TelegramNotifier(context.bot, update.effective_chat.id).send(OutgoingMessage(GREETING))

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        services: ChatServices = context.bot_data[SERVICES_KEY]
        chat_id = update.effective_chat.id
        notifier = TelegramNotifier(context.bot, chat_id)
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        try:
            reply = await services.assistant.reply(update.effective_message.text, services.clock())
        except Exception as exc:
            logger.error("답변 생성 실패: %s", type(exc).__name__, exc_info=not isinstance(exc, _TRANSIENT_ERRORS))
            await notifier.send(OutgoingMessage(FALLBACK_REPLY))
            return
        await notifier.send(OutgoingMessage(reply.text))
        for action in reply.confirmations:
            await notifier.send(confirmation_message(action))

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.from_user.id != self._allowed_user_id:
            return
        parsed = parse_callback(query.data)
        if parsed is None:
            await query.answer()
            return
        text = await handle_confirmation(context.bot_data[SERVICES_KEY], *parsed)
        if text is None:
            await query.answer(ALREADY_HANDLED)
            return
        await query.answer()
        await query.edit_message_text(text)
