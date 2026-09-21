"""2단계 완료 기준: 급한 이벤트 즉시 발송, 조용한 시간 보류 후 06:30 발송."""

from datetime import UTC, datetime

from app.core.events import EventKind
from app.core.interfaces import GateAction
from app.scheduler.dispatcher import Dispatcher, render_event
from tests.conftest import kst, make_event


async def test_urgent_event_is_sent_immediately_and_recorded(dispatcher, notifier, log):
    now = kst(9, 17, 14)
    decision = await dispatcher.publish(make_event(urgent=True, body="내일 수업은 휴강입니다."), now)

    assert decision.action is GateAction.SEND_NOW
    assert [m.text for m in notifier.sent] == ["휴강 안내\n내일 수업은 휴강입니다."]
    assert (await log.get("e1")).sent_at == now


async def test_quiet_hours_event_is_held_then_sent_at_quiet_end(dispatcher, notifier):
    decision = await dispatcher.publish(make_event(urgent=True), kst(9, 17, 23, 30))
    assert decision.action is GateAction.HOLD
    assert notifier.sent == []

    assert await dispatcher.release_pending(kst(9, 18, 6, 0)) == 0
    assert notifier.sent == []

    assert await dispatcher.release_pending(kst(9, 18, 6, 30)) == 1
    assert [m.text for m in notifier.sent] == ["휴강 안내"]

    assert await dispatcher.release_pending(kst(9, 18, 7, 0)) == 0
    assert len(notifier.sent) == 1


async def test_batched_event_is_not_sent(dispatcher, notifier):
    decision = await dispatcher.publish(make_event(), kst(9, 17, 14))
    assert decision.action is GateAction.BATCH
    assert notifier.sent == []
    assert await dispatcher.release_pending(kst(9, 17, 15)) == 0


async def test_same_event_twice_is_sent_once(dispatcher, notifier):
    await dispatcher.publish(make_event(urgent=True), kst(9, 17, 14))
    second = await dispatcher.publish(make_event(urgent=True), kst(9, 17, 15))
    assert second.action is GateAction.DROP
    assert len(notifier.sent) == 1


async def test_failed_send_is_retried_by_release(dispatcher, notifier, log):
    notifier.fail = True
    try:
        await dispatcher.publish(make_event(urgent=True), kst(9, 17, 14))
    except ConnectionError:
        pass
    assert (await log.get("e1")).sent_at is None

    notifier.fail = False
    assert await dispatcher.release_pending(kst(9, 17, 14, 5)) == 1
    assert len(notifier.sent) == 1


async def test_failed_send_retry_during_quiet_hours_is_held(dispatcher, notifier, log):
    notifier.fail = True
    try:
        await dispatcher.publish(make_event(urgent=True), kst(9, 17, 22, 50))
    except ConnectionError:
        pass
    notifier.fail = False

    assert await dispatcher.release_pending(kst(9, 17, 23, 10)) == 0
    assert (await log.get("e1")).action is GateAction.HOLD
    assert await dispatcher.release_pending(kst(9, 18, 6, 30)) == 1


def test_render_event_shows_due_time_in_kst():
    due = datetime(2026, 9, 18, 14, 59, tzinfo=UTC)
    message = render_event(make_event(title="과제 제출", due_at=due))
    assert message.text == "과제 제출\n마감: 9월 18일(금) 23:59"
    assert "*" not in message.text


# --- 사용자가 정한 말투로 다시 쓰기 ---


class FakePhraser:
    def __init__(self, fail: bool = False) -> None:
        self.drafts: list[str] = []
        self.fail = fail

    async def phrase_alert(self, draft: str) -> str:
        self.drafts.append(draft)
        if self.fail:
            raise RuntimeError("모델 장애")
        return "길동님, 휴강 소식이 있어요."


def phrased(gate, log, notifier, phraser) -> Dispatcher:
    return Dispatcher(gate, log, notifier, phraser=phraser)


async def test_alerts_are_sent_in_the_users_voice(gate, log, notifier):
    phraser = FakePhraser()
    event = make_event("p1", urgent=True, body="9월 23일 휴강", meta={"buttons": [{"label": "확인", "data": "x"}]})
    await phrased(gate, log, notifier, phraser).publish(event, kst(9, 20, 10))

    assert phraser.drafts == ["휴강 안내\n9월 23일 휴강"]
    assert notifier.sent[0].text == "길동님, 휴강 소식이 있어요."
    assert notifier.sent[0].buttons  # 버튼은 그대로


async def test_briefings_are_not_rewritten_twice(gate, log, notifier):
    phraser = FakePhraser()
    event = make_event("b1", kind=EventKind.BRIEFING, body="아침 브리핑 본문")
    await phrased(gate, log, notifier, phraser).publish(event, kst(9, 20, 7))
    assert phraser.drafts == [] and "아침 브리핑 본문" in notifier.sent[0].text


async def test_the_draft_goes_out_when_rewriting_fails(gate, log, notifier):
    event = make_event("p2", urgent=True)
    await phrased(gate, log, notifier, FakePhraser(fail=True)).publish(event, kst(9, 20, 10))
    assert notifier.sent[0].text == "휴강 안내"


async def test_batched_events_are_not_rewritten(gate, log, notifier):
    """브리핑으로 미루는 소식은 지금 보내지 않으므로 모델을 부르지 않는다."""
    phraser = FakePhraser()
    await phrased(gate, log, notifier, phraser).publish(make_event("p3"), kst(9, 20, 10))
    assert phraser.drafts == [] and notifier.sent == []
