import pytest

from app.core.events import Event, EventKind, EventSource
from app.core.interfaces import BriefingKind
from app.mail.service import MailService, parse_undo, undo_data
from app.scheduler.briefing import MailBriefing
from app.scheduler.dispatcher import render_event
from app.storage.mail import MailCleanupLog, MailStateStore, WaitingReplyStore
from tests.conftest import kst
from tests.mail.fakes import FakeAccount, FakeAccounts, FakeGmail, api_error

NOW = kst(9, 18, 22)


@pytest.fixture
def setup(db):
    gmail = FakeGmail()
    accounts = FakeAccounts(FakeAccount("개인", gmail, default=True))
    cleanup, waiting, state = MailCleanupLog(db), WaitingReplyStore(db), MailStateStore(db)
    service = MailService(accounts, cleanup, waiting, state, lambda: NOW)
    return {
        "service": service,
        "briefing": MailBriefing(service),
        "gmail": gmail,
        "cleanup": cleanup,
        "waiting": waiting,
        "state": state,
    }


# --- 되돌리기 ---


def test_undo_data_round_trip():
    data = undo_data(NOW)
    assert data == "undo:mail:20260918"
    assert parse_undo(data).date() == kst(9, 18, 0).date()
    assert parse_undo("undo:mail:not-a-day") is None
    assert parse_undo("confirm:abc") is None


async def test_undo_restores_todays_cleanup(setup):
    await setup["cleanup"].record("개인", "m-1", "가을 특가", "shop@example.com", kst(9, 18, 9))
    await setup["cleanup"].record("개인", "m-2", "세일 안내", "shop@example.com", kst(9, 18, 10))

    restored, failed = await setup["service"].undo_cleanup(kst(9, 18, 0))

    assert (restored, failed) == (2, 0)
    assert setup["gmail"].untrashed == ["m-1", "m-2"]
    # 두 번 눌러도 다시 되돌리지 않는다
    assert await setup["service"].undo_cleanup(kst(9, 18, 0)) == (0, 0)


async def test_undo_reports_failures_without_stopping(setup):
    await setup["cleanup"].record("개인", "m-1", "특가", "shop@example.com", kst(9, 18, 9))
    setup["gmail"].fail_with = api_error()

    restored, failed = await setup["service"].undo_cleanup(kst(9, 18, 0))
    assert (restored, failed) == (0, 1)


async def test_undo_skips_disconnected_accounts(db):
    gmail = FakeGmail()
    accounts = FakeAccounts(FakeAccount("개인", gmail, connected=False))
    cleanup = MailCleanupLog(db)
    service = MailService(accounts, cleanup, WaitingReplyStore(db), MailStateStore(db), lambda: NOW)
    await cleanup.record("개인", "m-1", "특가", "shop@example.com", kst(9, 18, 9))

    assert await service.undo_cleanup(kst(9, 18, 0)) == (0, 1)
    assert gmail.untrashed == []


# --- 브리핑 ---


async def test_morning_briefing_lists_other_mail_once(setup):
    await setup["state"].mark_seen("개인", "m-1", "other", "주말 약속", kst(9, 18, 7))
    await setup["state"].mark_seen("개인", "m-2", "other", "학회 안내", kst(9, 18, 8))

    items = await setup["briefing"].briefing_items(BriefingKind.MORNING, kst(9, 18, 7))
    assert [item.section for item in items] == ["새 메일", "새 메일"]
    assert "주말 약속" in items[0].text

    await setup["briefing"].acknowledge(kst(9, 18, 7))
    assert await setup["briefing"].briefing_items(BriefingKind.MORNING, kst(9, 18, 7)) == []


async def test_evening_briefing_summarises_cleanup_with_an_undo_button(setup):
    await setup["cleanup"].record("개인", "m-1", "가을 특가 안내", "shop@example.com", kst(9, 18, 9))

    items = await setup["briefing"].briefing_items(BriefingKind.EVENING, NOW)
    assert items[0].section == "메일 정리" and "1건을 휴지통으로" in items[0].text

    buttons = await setup["briefing"].briefing_buttons(BriefingKind.EVENING, NOW)
    assert buttons == [{"label": "메일 정리 되돌리기", "data": "undo:mail:20260918"}]
    # 정리한 것이 없으면 버튼도 없다
    assert await setup["briefing"].briefing_buttons(BriefingKind.MORNING, NOW) == []


async def test_overdue_reply_is_reminded_once(setup):
    await setup["waiting"].add(
        "개인", "thread-1", "m-1", "면담 일정", "prof@example.ac.kr", kst(9, 15, 9), due_at=kst(9, 18, 9)
    )

    items = await setup["briefing"].briefing_items(BriefingKind.EVENING, NOW)
    assert any(item.section == "답장이 아직 안 나간 메일" for item in items)

    await setup["briefing"].acknowledge(NOW)
    again = await setup["briefing"].briefing_items(BriefingKind.EVENING, NOW)
    assert all(item.section != "답장이 아직 안 나간 메일" for item in again)


async def test_weekly_plan_has_no_mail_items(setup):
    await setup["state"].mark_seen("개인", "m-1", "other", "주말 약속", kst(9, 18, 7))
    assert await setup["briefing"].briefing_items(BriefingKind.WEEKLY, NOW) == []


# --- 알림에 붙는 버튼 ---


def test_event_buttons_are_sent_with_the_message():
    event = Event(
        source=EventSource.SCHEDULER,
        kind=EventKind.BRIEFING,
        title="저녁 브리핑",
        body="메일 정리 1건",
        ref_id="briefing:evening:20260918",
        meta={"buttons": [{"label": "메일 정리 되돌리기", "data": "undo:mail:20260918"}]},
    )
    message = render_event(event)
    assert [(button.label, button.callback_data) for button in message.buttons] == [
        ("메일 정리 되돌리기", "undo:mail:20260918")
    ]
    plain = render_event(
        Event(source=EventSource.SCHEDULER, kind=EventKind.BRIEFING, title="아침 브리핑", ref_id="x")
    )
    assert plain.buttons == ()
