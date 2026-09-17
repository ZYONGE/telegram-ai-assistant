"""2단계 완료 기준: 급한 이벤트 즉시 발송, 조용한 시간 보류 후 06:30 발송."""

from datetime import UTC, datetime

from app.core.interfaces import GateAction
from app.scheduler.dispatcher import render_event
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
