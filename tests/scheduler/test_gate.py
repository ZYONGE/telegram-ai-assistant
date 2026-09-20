from datetime import datetime, timedelta

import pytest

from app.core.config import NotificationSettings
from app.core.events import collector_failed
from app.core.interfaces import GateAction, GateDecision
from app.scheduler.gate import RuleBasedGate
from tests.conftest import kst, make_event

# 낮 시간. 조용한 시간에 걸리지 않게 한다.
DAY = kst(9, 17, 14)


async def test_urgent_event_in_daytime_is_sent_now(gate):
    decision = await gate.decide(make_event(urgent=True), kst(9, 17, 14))
    assert decision.action is GateAction.SEND_NOW


async def test_normal_event_is_batched(gate):
    decision = await gate.decide(make_event(), kst(9, 17, 14))
    assert decision.action is GateAction.BATCH


@pytest.mark.parametrize(
    ("now", "release"),
    [
        (kst(9, 17, 23, 0), kst(9, 18, 6, 30)),
        (kst(9, 17, 23, 45), kst(9, 18, 6, 30)),
        (kst(9, 18, 2, 0), kst(9, 18, 6, 30)),
        (kst(9, 18, 6, 29), kst(9, 18, 6, 30)),
    ],
)
async def test_quiet_hours_hold_until_quiet_end(gate, now, release):
    decision = await gate.decide(make_event(urgent=True), now)
    assert decision.action is GateAction.HOLD
    assert decision.release_at == release


@pytest.mark.parametrize("now", [kst(9, 17, 22, 59), kst(9, 18, 6, 30)])
async def test_quiet_hours_boundaries_are_not_quiet(gate, now):
    decision = await gate.decide(make_event(urgent=True), now)
    assert decision.action is GateAction.SEND_NOW


async def test_user_requested_reminder_ignores_quiet_hours(gate):
    decision = await gate.decide(make_event(user_requested=True), kst(9, 18, 1, 0))
    assert decision.action is GateAction.SEND_NOW


async def test_duplicate_event_is_dropped(gate, log):
    event = make_event()
    await log.save_decision(event, GateDecision(GateAction.BATCH, "묶음"), kst(9, 17, 14))
    decision = await gate.decide(event, kst(9, 17, 15))
    assert decision.action is GateAction.DROP


async def test_held_event_is_dropped_before_release_and_redecided_after(gate, log):
    event = make_event(urgent=True)
    hold = await gate.decide(event, kst(9, 17, 23, 30))
    await log.save_decision(event, hold, kst(9, 17, 23, 30))

    assert (await gate.decide(event, kst(9, 18, 3))).action is GateAction.DROP
    assert (await gate.decide(event, kst(9, 18, 6, 30))).action is GateAction.SEND_NOW


async def test_sent_event_is_never_redecided(gate, log):
    event = make_event(urgent=True)
    await log.save_decision(event, GateDecision(GateAction.SEND_NOW, "급함"), kst(9, 17, 14))
    await log.mark_sent(event.ref_id, kst(9, 17, 14))
    assert (await gate.decide(event, kst(9, 17, 15))).action is GateAction.DROP


async def test_unsent_send_now_event_is_redecided(gate, log):
    event = make_event(urgent=True)
    await log.save_decision(event, GateDecision(GateAction.SEND_NOW, "급함"), kst(9, 17, 14))
    assert (await gate.decide(event, kst(9, 17, 14, 5))).action is GateAction.SEND_NOW


async def _record_sent(log, ref_id, at, **overrides):
    event = make_event(ref_id, urgent=True, **overrides)
    await log.save_decision(event, GateDecision(GateAction.SEND_NOW, "급함"), at)
    await log.mark_sent(ref_id, at)


async def test_daily_limit_moves_urgent_events_to_batch(gate, log):
    # settings.daily_limit == 2
    await _record_sent(log, "a", kst(9, 17, 8))
    await _record_sent(log, "b", kst(9, 17, 9))

    decision = await gate.decide(make_event("c", urgent=True), kst(9, 17, 14))
    assert decision.action is GateAction.BATCH
    assert "상한" in decision.reason


async def test_daily_limit_excludes_user_requested_and_previous_days(gate, log):
    await _record_sent(log, "yesterday", kst(9, 16, 23, 59))
    await _record_sent(log, "early", kst(9, 17, 0, 0))
    await _record_sent(log, "reminder", kst(9, 17, 9), user_requested=True)

    assert (await gate.decide(make_event("c", urgent=True), kst(9, 17, 14))).action is GateAction.SEND_NOW


async def test_user_requested_reminder_ignores_daily_limit(gate, log):
    await _record_sent(log, "a", kst(9, 17, 8))
    await _record_sent(log, "b", kst(9, 17, 9))
    decision = await gate.decide(make_event("c", user_requested=True), kst(9, 17, 14))
    assert decision.action is GateAction.SEND_NOW


async def test_naive_now_is_rejected(gate):
    with pytest.raises(ValueError):
        await gate.decide(make_event(), datetime(2026, 9, 17, 14))


# --- 수집 실패 되풀이 알림 ---


async def test_a_failure_that_keeps_happening_is_told_about_again(gate, log):
    """며칠째 수집이 안 되는데 조용한 것이 가장 나쁘다."""
    event = collector_failed("eclass", "login", "로그인 실패")
    first = await gate.decide(event, DAY)
    await log.save_decision(event, first, DAY)
    await log.mark_sent(event.ref_id, DAY)

    # 6시간이 지나기 전에는 조용하다
    soon = await gate.decide(event, DAY + timedelta(hours=5))
    assert soon.action is GateAction.DROP

    again = await gate.decide(event, DAY + timedelta(hours=7))
    assert again.action is not GateAction.DROP


async def test_a_failure_we_already_told_about_stays_quiet_for_a_while(gate, log):
    event = collector_failed("eclass", "login", "로그인 실패")
    decision = await gate.decide(event, DAY)
    await log.save_decision(event, decision, DAY)
    await log.mark_sent(event.ref_id, DAY)

    assert (await gate.decide(event, DAY + timedelta(minutes=30))).action is GateAction.DROP


async def test_other_news_is_never_repeated(gate, log):
    """공지는 한 번이면 된다. 되풀이하는 것은 수집 실패뿐이다."""
    event = make_event("notice-1")
    decision = await gate.decide(event, DAY)
    await log.save_decision(event, decision, DAY)
    await log.mark_sent(event.ref_id, DAY)

    assert (await gate.decide(event, DAY + timedelta(days=3))).action is GateAction.DROP


async def test_repeating_can_be_turned_off(log):
    off = RuleBasedGate(log, NotificationSettings(failure_repeat_hours=0))
    event = collector_failed("eclass", "login", "로그인 실패")
    decision = await off.decide(event, DAY)
    await log.save_decision(event, decision, DAY)
    await log.mark_sent(event.ref_id, DAY)

    assert (await off.decide(event, DAY + timedelta(days=2))).action is GateAction.DROP
