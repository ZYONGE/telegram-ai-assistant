"""텔레그램 수신: 허용된 사용자 한 명의 메시지와 확인 버튼만 처리한다. 그 외는 응답하지 않는다.

사람다운 말이 이 비서의 핵심이다 (CLAUDE.md 7절). 여기 적힌 문장(위치 저장, 버튼 처리 결과, 인사 등)은
**전할 사실을 적은 초안**이고, 보낼 때 가벼운 모델이 상황에 맞게 다시 쓴다. 모델을 못 부르면 초안을 그대로 보낸다.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from app.agent.loop import Assistant
from app.agent.prompt import DEFAULT_HONORIFIC
from app.channels.telegram import TelegramNotifier
from app.core.clock import utc_now
from app.core.interfaces import Button, OutgoingMessage
from app.llm import TransientLLMError
from app.mail.service import MailService, parse_undo
from app.storage.conversation import ConversationStore, PendingAction
from app.storage.location import LocationStore
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

SERVICES_KEY = "services"
CONFIRM, CANCEL = "confirm", "cancel"
FALLBACK_REPLY = "지금은 답변을 만들 수 없습니다. 잠시 후 다시 말씀해 주세요."
ALREADY_HANDLED = "이미 처리된 요청입니다."
GREETING = "{honorific}, 비서가 준비되었습니다. 할 일이나 리마인더를 편하게 말씀해 주세요."
LOCATION_SAVED = "위치를 받았습니다. 이제 이 위치를 기준으로 날씨를 알려 드립니다."
LOCATION_LIVE = "실시간 위치 공유를 받았습니다. 공유하는 동안 위치를 따라가며 날씨를 봅니다."
LOCATION_OFF = "위치 기반 날씨가 꺼져 있습니다. config.toml의 [weather] follow_telegram_location을 확인하세요."
LOCATION_HELP = (
    "아래 [위치 보내기] 버튼을 누르면 지금 위치가 전송됩니다. 버튼은 대화창에 계속 남아 있어 누를 때마다 갱신됩니다.\n"
    "이동 중에도 따라가게 하려면 클립(첨부) → 위치 → 실시간 위치 공유를 켜 주세요. 켜 두는 동안 자동으로 갱신됩니다.\n"
    "한 번 받은 위치는 다음에 보내실 때까지 계속 기준으로 씁니다."
)
LOCATION_HIDDEN = "위치 버튼을 치웠습니다. 다시 띄우려면 /location 을 보내 주세요."
HIDE_WORDS = {"off", "끄기", "숨기기", "치워"}
UNDO_DONE = "되돌렸습니다: 메일 {restored}건을 받은편지함으로 되돌렸습니다."
UNDO_NOTHING = "되돌릴 메일이 없습니다."


class Voice(Protocol):
    """사실을 상황에 맞는 말로 바꿔 준다 (app/agent/light.py의 LightModel.say)."""

    async def say(self, draft: str, situation: str) -> str: ...


@dataclass(slots=True)
class ChatServices:
    assistant: Assistant
    registry: ToolRegistry
    conversation: ConversationStore
    clock: Callable[[], datetime] = utc_now
    # 사용자가 보낸 위치를 저장한다. 꺼져 있으면 None.
    location: LocationStore | None = None
    # 메일 정리 되돌리기에 쓴다. Google 연결 전에는 None.
    mail: MailService | None = None
    # 정해 둔 초안을 사람의 말로 바꾼다. 없으면 초안을 그대로 보낸다.
    voice: Voice | None = None


async def speak(services: ChatServices | None, draft: str, situation: str) -> str:
    """초안을 상황에 맞는 말로. 실패하거나 목소리가 없으면 초안 그대로."""
    voice = services.voice if services is not None else None
    if voice is None:
        return draft
    try:
        return (await voice.say(draft, situation)).strip() or draft
    except Exception:
        logger.exception("대답 다듬기 실패, 초안을 보냅니다")
        return draft


def confirm_buttons(action: PendingAction) -> tuple[Button, ...]:
    return (Button("확인", f"{CONFIRM}:{action.id}"), Button("취소", f"{CANCEL}:{action.id}"))


def confirmation_message(action: PendingAction) -> OutgoingMessage:
    """확인할 것이 여럿일 때 하나씩 따로 보내는 메시지. 무엇을 누르는지 구별되게 요약만 싣는다."""
    return OutgoingMessage(action.summary, buttons=confirm_buttons(action))


def parse_callback(data: str | None) -> tuple[str, str] | None:
    verb, _, action_id = (data or "").partition(":")
    if verb in (CONFIRM, CANCEL) and action_id:
        return verb, action_id
    return None


async def handle_undo(services: ChatServices, day_start: datetime) -> str:
    """저녁 브리핑의 [메일 정리 되돌리기] 처리."""
    if services.mail is None:
        return UNDO_NOTHING
    restored, failed = await services.mail.undo_cleanup(day_start)
    if not restored and not failed:
        return UNDO_NOTHING
    text = UNDO_DONE.format(restored=restored)
    if failed:
        text += f" {failed}건은 되돌리지 못했습니다."
    await services.conversation.add_note(f"메일 정리 되돌리기: {restored}건 복구", services.clock())
    return text


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
    def __init__(self, allowed_user_id: int, honorific: str = DEFAULT_HONORIFIC) -> None:
        self._allowed_user_id = allowed_user_id
        self._honorific = honorific

    def register(self, application: Application) -> None:
        only_owner = filters.User(user_id=self._allowed_user_id) & filters.ChatType.PRIVATE
        application.add_handler(CommandHandler("start", self.on_start, filters=only_owner))
        application.add_handler(CommandHandler("location", self.on_location_command, filters=only_owner))
        application.add_handler(MessageHandler(only_owner & filters.TEXT & ~filters.COMMAND, self.on_text))
        # 위치 메시지와 실시간 공유 갱신(edited_message)을 함께 받는다
        application.add_handler(MessageHandler(only_owner & filters.LOCATION, self.on_location))
        application.add_handler(CallbackQueryHandler(self.on_callback))

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        services: ChatServices | None = context.bot_data.get(SERVICES_KEY)
        greeting = await speak(
            services, GREETING.format(honorific=self._honorific), "사용자가 대화를 처음 시작했다(/start). 반갑게 맞는다"
        )
        # 위치를 아직 한 번도 안 보냈으면 처음부터 버튼을 띄워 둔다
        need_location = (
            services is not None and services.location is not None and await services.location.latest() is None
        )
        text = f"{greeting}\n\n{LOCATION_HELP}" if need_location else greeting
        message = OutgoingMessage(text, request_location=need_location)
        await TelegramNotifier(context.bot, update.effective_chat.id).send(message)

    async def on_location_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """위치 버튼을 띄우거나(기본) 치운다(/location 끄기)."""
        services: ChatServices = context.bot_data[SERVICES_KEY]
        notifier = TelegramNotifier(context.bot, update.effective_chat.id)
        if services.location is None:
            await notifier.send(OutgoingMessage(LOCATION_OFF))
            return
        argument = (update.effective_message.text or "").partition(" ")[2].strip().lower()
        if argument in HIDE_WORDS:
            await notifier.send(OutgoingMessage(LOCATION_HIDDEN, remove_keyboard=True))
            return
        await notifier.send(OutgoingMessage(LOCATION_HELP, request_location=True))

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        services: ChatServices = context.bot_data[SERVICES_KEY]
        chat_id = update.effective_chat.id
        notifier = TelegramNotifier(context.bot, chat_id)
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        async def progress(text: str) -> None:
            # 도구를 쓰는 요청이면 끝나기 전에 받았다는 말을 먼저 보낸다 (사용자 지시)
            await notifier.send(OutgoingMessage(text))
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        try:
            reply = await services.assistant.reply(update.effective_message.text, services.clock(), progress)
        except Exception as exc:
            # 일시적인 모델 장애는 스택 추적 없이 한 줄만 남긴다
            transient = isinstance(exc, TransientLLMError)
            logger.error("답변 생성 실패: %s %s", type(exc).__name__, exc if transient else "", exc_info=not transient)
            await notifier.send(OutgoingMessage(FALLBACK_REPLY))
            return
        if len(reply.confirmations) == 1:
            # 확인할 것이 하나면 비서의 말 아래에 버튼을 바로 단다. 따로 정해 둔 문구를 한 번 더 보내지 않는다.
            await notifier.send(OutgoingMessage(reply.text, buttons=confirm_buttons(reply.confirmations[0])))
            return
        await notifier.send(OutgoingMessage(reply.text))
        for action in reply.confirmations:
            await notifier.send(confirmation_message(action))

    async def on_location(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """사용자가 보낸 위치를 저장한다. 좌표는 로그에 남기지 않는다."""
        services: ChatServices = context.bot_data[SERVICES_KEY]
        message = update.effective_message
        location = getattr(message, "location", None)
        if location is None:
            return
        notifier = TelegramNotifier(context.bot, update.effective_chat.id)
        if services.location is None:
            await notifier.send(OutgoingMessage(LOCATION_OFF))
            return

        now = services.clock()
        live_period = getattr(location, "live_period", None)
        live_until = now + timedelta(seconds=live_period) if live_period else None
        await services.location.save(location.latitude, location.longitude, now, live_until)
        logger.info("위치를 갱신했습니다 (실시간 공유: %s)", bool(live_period))
        # 실시간 공유 중 자동 갱신에는 답하지 않는다
        if update.edited_message is not None:
            return
        # 버튼은 그대로 두어 다음에도 한 번에 보낼 수 있게 한다
        text = await speak(
            services,
            LOCATION_LIVE if live_period else LOCATION_SAVED,
            "사용자가 텔레그램으로 위치를 보내 줘서 날씨 기준 위치로 저장했다",
        )
        await notifier.send(OutgoingMessage(text, request_location=True))

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.from_user.id != self._allowed_user_id:
            return
        services: ChatServices = context.bot_data[SERVICES_KEY]
        undo_day = parse_undo(query.data or "")
        if undo_day is not None:
            await query.answer()
            await _reply_under(query, context, services, await handle_undo(services, undo_day),
                               "사용자가 저녁 브리핑의 메일 정리 되돌리기 버튼을 눌렀고 그 결과를 알린다")
            return

        parsed = parse_callback(query.data)
        if parsed is None:
            await query.answer()
            return
        text = await handle_confirmation(services, *parsed)
        if text is None:
            await query.answer(ALREADY_HANDLED)
            return
        await query.answer()
        await _reply_under(query, context, services, text, "사용자가 확인 또는 취소 버튼을 눌렀고 그 처리 결과를 알린다")


async def _reply_under(query, context, services: ChatServices, draft: str, situation: str) -> None:
    """버튼을 치우고, 처리 결과는 비서의 말로 새로 보낸다. 원래 메시지(비서가 한 말)는 그대로 둔다."""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        logger.info("버튼을 치우지 못했습니다")
    text = await speak(services, draft, situation)
    await TelegramNotifier(context.bot, query.message.chat_id).send(OutgoingMessage(text))
