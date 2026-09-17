"""선제 알림의 유일한 발송 경로: 게이트 결정 → 기록 → (즉시 발송이면) 전송."""

import logging
from datetime import datetime

from app.core.clock import format_kst
from app.core.events import Event
from app.core.interfaces import GateAction, GateDecision, NotificationGate, Notifier, OutgoingMessage
from app.storage.notifications import NotificationLog

logger = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, gate: NotificationGate, log: NotificationLog, notifier: Notifier) -> None:
        self._gate = gate
        self._log = log
        self._notifier = notifier

    async def publish(self, event: Event, now: datetime) -> GateDecision:
        decision = await self._gate.decide(event, now)
        if decision.action is GateAction.DROP:
            return decision
        await self._log.save_decision(event, decision, now)
        if decision.action is GateAction.SEND_NOW:
            await self._notifier.send(render_event(event))
            await self._log.mark_sent(event.ref_id, now)
        return decision

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
    return OutgoingMessage("\n".join(lines))
