"""알림 게이트: 모든 선제 이벤트의 발송 여부를 코드 규칙으로 결정한다.

판단 순서
1. 이미 처리한 이벤트(같은 ref_id) → drop
   단, 보류 시각이 지났거나 즉시 발송에 실패한 이벤트는 다시 판단한다
2. 조용한 시간(기본 23:00~06:30 KST) → hold, 조용한 시간이 끝나는 시각에 다시 판단
   사용자가 직접 요청한 알림은 예외
3. 사용자가 요청한 알림, 정해진 브리핑 → send_now (일일 상한에 세지 않음)
4. 급한 이벤트 → send_now, 오늘 상한을 넘었으면 batch
5. 나머지 → batch (다음 브리핑에 묶음)
"""

from datetime import UTC, datetime, time, timedelta

from app.core.clock import KST, require_aware, to_kst
from app.core.config import NotificationSettings
from app.core.events import Event, EventKind
from app.core.interfaces import GateAction, GateDecision
from app.storage.notifications import NotificationLog, NotificationRecord


class RuleBasedGate:
    def __init__(self, log: NotificationLog, settings: NotificationSettings) -> None:
        self._log = log
        self._settings = settings

    async def decide(self, event: Event, now: datetime) -> GateDecision:
        require_aware(now, "now")
        previous = await self._log.get(event.ref_id)
        if previous is not None and not _needs_redecision(previous, now):
            return GateDecision(GateAction.DROP, "이미 처리한 이벤트")

        if not event.user_requested and self.in_quiet_hours(now):
            return GateDecision(GateAction.HOLD, "조용한 시간", release_at=self.quiet_end_after(now))

        if event.user_requested:
            return GateDecision(GateAction.SEND_NOW, "사용자가 요청한 알림")

        if event.kind == EventKind.BRIEFING:
            return GateDecision(GateAction.SEND_NOW, "정해진 브리핑")

        if event.urgent:
            sent_today = await self._log.count_sent_since(_start_of_kst_day(now))
            if sent_today >= self._settings.daily_limit:
                return GateDecision(GateAction.BATCH, "오늘 선제 알림 상한 도달")
            return GateDecision(GateAction.SEND_NOW, "급한 소식")

        return GateDecision(GateAction.BATCH, "다음 브리핑에 묶음")

    def in_quiet_hours(self, now: datetime) -> bool:
        current = to_kst(now).time()
        start, end = self._settings.quiet_start, self._settings.quiet_end
        if start <= end:
            return start <= current < end
        return current >= start or current < end

    def quiet_end_after(self, now: datetime) -> datetime:
        local = to_kst(now)
        release = datetime.combine(local.date(), self._settings.quiet_end, tzinfo=KST)
        if release <= local:
            release += timedelta(days=1)
        return release.astimezone(UTC)


def _needs_redecision(previous: NotificationRecord, now: datetime) -> bool:
    if previous.sent_at is not None:
        return False
    if previous.action is GateAction.HOLD:
        return previous.release_at is not None and previous.release_at <= now
    return previous.action is GateAction.SEND_NOW


def _start_of_kst_day(now: datetime) -> datetime:
    return datetime.combine(to_kst(now).date(), time(0), tzinfo=KST)
