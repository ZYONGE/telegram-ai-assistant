"""선제 알림의 유일한 발송 경로: 게이트 결정 → 기록 → (즉시 발송이면) 문장 다듬기 → 전송.

알림 초안은 코드가 만들고, 보낼 때만 가벼운 모델이 사용자가 정한 말투로 다시 쓴다 (알릴 내용이 있을 때만 모델을 부른다).
브리핑은 이미 다듬어져 오므로 다시 쓰지 않는다. 다듬기에 실패하면 초안을 그대로 보낸다.
"""

import logging
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from app.core.clock import format_kst
from app.core.events import Event, EventKind
from app.core.interfaces import Button, GateAction, GateDecision, NotificationGate, Notifier, OutgoingMessage
from app.storage.notifications import NotificationLog

logger = logging.getLogger(__name__)


class Phraser(Protocol):
    async def phrase_alert(self, draft: str) -> str: ...


class Dispatcher:
    def __init__(
        self,
        gate: NotificationGate,
        log: NotificationLog,
        notifier: Notifier,
        phraser: Phraser | None = None,
    ) -> None:
        self._gate = gate
        self._log = log
        self._notifier = notifier
        self._phraser = phraser

    async def publish(self, event: Event, now: datetime) -> GateDecision:
        decision = await self._gate.decide(event, now)
        if decision.action is GateAction.DROP:
            return decision
        await self._log.save_decision(event, decision, now)
        if decision.action is GateAction.SEND_NOW:
            await self._notifier.send(await self._message(event))
            await self._log.mark_sent(event.ref_id, now)
        return decision

    async def _message(self, event: Event) -> OutgoingMessage:
        message = render_event(event)
        if self._phraser is None or event.kind == EventKind.BRIEFING:
            return message
        try:
            text = (await self._phraser.phrase_alert(message.text)).strip()
        except Exception:
            logger.exception("알림 다듬기 실패, 초안을 보냅니다")
            return message
        return replace(message, text=text or message.text)

    async def release_pending(self, now: datetime) -> int:
        """보류가 풀렸거나 발송에 실패했던 알림을 다시 판단한다. 보낸 건수를 반환한다."""
        sent = 0
        for record in await self._log.pending_delivery(now):
            try:
                decision = await self.publish(record.event, now)
            except Exception:
                logger.exception("보류 알림 재처리 실패: %s", record.event.ref_id)
                continue
            if decision.action is GateAction.SEND_NOW:
                sent += 1
        return sent


def render_event(event: Event) -> OutgoingMessage:
    """모델 없이 만드는 기본 알림 문장. 마크다운 서식은 쓰지 않는다."""
    lines = [event.title]
    if event.body:
        lines.append(event.body)
    if event.due_at is not None:
        lines.append(f"마감: {format_kst(event.due_at)}")
    # meta에 버튼이 있으면 함께 보낸다 (예: 저녁 브리핑의 메일 정리 되돌리기)
    buttons = tuple(
        Button(str(item["label"]), str(item["data"]))
        for item in event.meta.get("buttons", [])
        if isinstance(item, dict) and item.get("label") and item.get("data")
    )
    return OutgoingMessage("\n".join(lines), buttons=buttons)
