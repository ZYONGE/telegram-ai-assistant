from datetime import timedelta

import pytest

from app.core.clock import to_kst
from app.core.events import EventKind
from app.core.interfaces import BriefingItem, BriefingKind, GateAction, GateDecision
from app.scheduler.briefing import BriefingService, NewsBriefing, TaskBriefing, TodoBriefing, compose
from tests.conftest import kst, make_event


class FakePolisher:
    def __init__(self, result=None):
        self.drafts = []
        self.result = result

    async def polish_briefing(self, kind, draft):
        self.drafts.append(draft)
        return self.result or draft


@pytest.fixture
def providers(todos, task_service, log):
    return [TodoBriefing(todos), TaskBriefing(task_service), NewsBriefing(log)]


async def seed(todos, task_service, log, clock):
    now = kst(9, 17, 6)
    await todos.add("오늘 낼 보고서", now, due_at=kst(9, 17, 23, 59))
    await todos.add("지난 과제", now, due_at=kst(9, 16, 23, 59))
    await todos.add("모레 시험", now, due_at=kst(9, 19, 10))
    await todos.add("내일 발표", now, due_at=kst(9, 18, 13))
    await todos.add("마감 없는 일", now)
    await task_service.create("reminder", "약 먹기", kst(9, 17, 6), run_at=kst(9, 17, 12))
    await task_service.create("reminder", "병원 예약", kst(9, 17, 6), run_at=kst(9, 18, 9))
    await log.save_decision(make_event("n1", title="자료구조 공지"), GateDecision(GateAction.BATCH, "묶음"), now)


async def test_morning_briefing_content(todos, task_service, log, clock, providers, dispatcher, notifier):
    await seed(todos, task_service, log, clock)
    polisher = FakePolisher()
    decision = await BriefingService(providers, dispatcher, polisher).send(BriefingKind.MORNING, kst(9, 17, 7))

    assert decision.action is GateAction.SEND_NOW
    text = notifier.sent[0].text
    assert text.startswith("사용자님, 좋은 아침입니다. 9월 17일(목) 브리핑입니다.")  # 제목 줄 없이
    sections = [line for line in text.splitlines() if line and not line.startswith(("·", "사용자님", "아침"))]
    assert sections == ["오늘 마감", "마감이 지난 할 일", "오늘 리마인더", "새 소식", "다가오는 마감 (3일 이내)"]
    assert "· 오늘 낼 보고서 (마감 9월 17일(목) 23:59)" in text
    assert "· 1건: 지난 과제" in text
    assert "· 12:00 약 먹기" in text
    assert "· 자료구조 공지" in text
    assert "모레 시험" in text and "내일 발표" in text
    assert "마감 없는 일" not in text and "병원 예약" not in text
    assert len(polisher.drafts) == 1


async def test_evening_briefing_content(todos, task_service, log, clock, providers, dispatcher, notifier):
    await seed(todos, task_service, log, clock)
    done = await todos.add("끝낸 일", kst(9, 17, 8))
    await todos.set_status(done.id, "done", kst(9, 17, 20))

    await BriefingService(providers, dispatcher, None).send(BriefingKind.EVENING, kst(9, 17, 22))
    text = notifier.sent[0].text
    assert "내일 마감\n· 내일 발표 (마감 9월 18일(금) 13:00)" in text
    assert "오늘 마감인데 남은 일\n· 오늘 낼 보고서" in text
    assert "내일 리마인더\n· 09:00 병원 예약" in text
    assert "오늘 한 일\n· 할 일 1건 완료: 끝낸 일" in text


async def test_batched_news_appears_in_only_one_briefing(providers, log, dispatcher, notifier):
    await log.save_decision(make_event("n1", title="학과 공지"), GateDecision(GateAction.BATCH, "묶음"), kst(9, 17, 6))
    service = BriefingService(providers, dispatcher, None)
    await service.send(BriefingKind.MORNING, kst(9, 17, 7))
    await service.send(BriefingKind.EVENING, kst(9, 17, 22))
    assert "학과 공지" in notifier.sent[0].text
    assert "학과 공지" not in notifier.sent[1].text


async def test_empty_day_skips_polisher(providers, dispatcher, notifier):
    polisher = FakePolisher("다듬음")
    await BriefingService(providers, dispatcher, polisher).send(BriefingKind.MORNING, kst(9, 17, 7))
    assert polisher.drafts == []
    assert notifier.sent[0].text.endswith("오늘 따로 챙길 마감이나 소식은 없습니다.")


async def test_polished_text_is_sent(todos, providers, dispatcher, notifier):
    await todos.add("보고서", kst(9, 17, 6), due_at=kst(9, 17, 18))
    await BriefingService(providers, dispatcher, FakePolisher("사용자님, 오늘은 보고서 하나입니다.")).send(
        BriefingKind.MORNING, kst(9, 17, 7)
    )
    assert notifier.sent[0].text == "사용자님, 오늘은 보고서 하나입니다."


async def test_briefing_is_sent_once_per_day_and_not_counted(providers, dispatcher, notifier, log):
    service = BriefingService(providers, dispatcher, None)
    await service.send(BriefingKind.MORNING, kst(9, 17, 7))
    again = await service.send(BriefingKind.MORNING, kst(9, 17, 7, 1))
    assert again.action is GateAction.DROP
    assert len(notifier.sent) == 1
    assert await log.count_sent_since(kst(9, 17, 0)) == 0
    assert (await log.get("briefing:morning:20260917")).event.kind == EventKind.BRIEFING


async def test_briefing_in_quiet_hours_is_held_and_news_kept(providers, dispatcher, notifier, log):
    await log.save_decision(make_event("n1", title="공지"), GateDecision(GateAction.BATCH, "묶음"), kst(9, 17, 6))
    decision = await BriefingService(providers, dispatcher, None).send(BriefingKind.EVENING, kst(9, 17, 23, 30))
    assert decision.action is GateAction.HOLD
    assert notifier.sent == []
    assert [r.event.ref_id for r in await log.unbriefed_batch()] == ["n1"]


async def test_failing_provider_does_not_block_briefing(dispatcher, notifier):
    class Broken:
        name = "broken"

        async def briefing_items(self, kind, now):
            raise RuntimeError("db locked")

    await BriefingService([Broken()], dispatcher, None).send(BriefingKind.MORNING, kst(9, 17, 7))
    assert len(notifier.sent) == 1


async def test_honorific_comes_from_settings(providers, dispatcher, notifier):
    await BriefingService(providers, dispatcher, None, honorific="길동님").send(BriefingKind.EVENING, kst(9, 17, 22))
    assert notifier.sent[0].text.startswith("길동님, 오늘 정리와 내일 준비 사항입니다.")


def test_compose_orders_sections_by_priority():
    items = [BriefingItem("낮음", "a", 1), BriefingItem("높음", "b", 9), BriefingItem("낮음", "c", 1)]
    text = compose(BriefingKind.EVENING, items, kst(9, 17, 22))
    assert text.splitlines()[1:] == ["", "높음", "· b", "", "낮음", "· a", "· c"]


async def seed_next_week(todos, task_service, log):
    """일요일 저녁 기준: 다음 주는 9월 21일(월)~27일(일)."""
    now = kst(9, 20, 9)
    await todos.add("월요일 보고서", now, due_at=kst(9, 21, 23, 59))
    await todos.add("수요일 발표", now, due_at=kst(9, 23, 13))
    await todos.add("일요일 정리", now, due_at=kst(9, 27, 23, 59))
    await todos.add("다다음 주 시험", now, due_at=kst(9, 28, 10))
    await todos.add("지난 과제", now, due_at=kst(9, 19, 23, 59))
    await todos.add("마감 없는 일", now)
    await task_service.create("reminder", "병원 예약", now, run_at=kst(9, 22, 9))
    await log.save_decision(make_event("n1", title="학과 공지"), GateDecision(GateAction.BATCH, "묶음"), now)


async def test_weekly_plan_covers_next_week_only(todos, task_service, log, providers, dispatcher, notifier):
    await seed_next_week(todos, task_service, log)
    polisher = FakePolisher()
    decision = await BriefingService(providers, dispatcher, polisher).send(BriefingKind.WEEKLY, kst(9, 20, 21))

    assert decision.action is GateAction.SEND_NOW
    text = notifier.sent[0].text
    assert text.startswith("사용자님, 다음 주 계획입니다.")
    sections = [line for line in text.splitlines() if line and not line.startswith(("·", "사용자님", "주간"))]
    assert sections == ["다음 주 마감", "아직 남은 일", "다음 주 리마인더", "마감 없는 할 일"]
    assert "월요일 보고서" in text and "수요일 발표" in text and "일요일 정리" in text
    assert "다다음 주 시험" not in text
    assert "· 지난 과제 (마감 9월 19일(토) 23:59)" in text
    assert "· 9월 22일(화) 09:00 병원 예약" in text
    assert "· 마감 없는 일" in text
    assert len(polisher.drafts) == 1


async def test_weekly_plan_leaves_news_for_the_daily_briefings(todos, task_service, log, providers, dispatcher, notifier):
    await seed_next_week(todos, task_service, log)
    await BriefingService(providers, dispatcher, None).send(BriefingKind.WEEKLY, kst(9, 20, 21))
    assert "학과 공지" not in notifier.sent[0].text
    assert [r.event.ref_id for r in await log.unbriefed_batch()] == ["n1"]


async def test_empty_week_says_so(providers, dispatcher, notifier):
    await BriefingService(providers, dispatcher, None).send(BriefingKind.WEEKLY, kst(9, 20, 21))
    assert notifier.sent[0].text.endswith("다음 주에 챙길 마감이나 예약은 없습니다.")


# --- 저녁 브리핑: 오늘 한 일, 이번 주 안에 할 일, 오늘 지난 일정 (사용자 지시 2026-09-21) ---


async def test_evening_lists_what_is_due_within_the_week(todos, task_service, log, clock, providers, dispatcher, notifier):
    await seed(todos, task_service, log, clock)
    await todos.add("다음 주 과제", kst(9, 17, 6), due_at=kst(9, 23, 23, 59))
    await todos.add("먼 마감", kst(9, 17, 6), due_at=kst(10, 30, 23, 59))

    await BriefingService(providers, dispatcher, None).send(BriefingKind.EVENING, kst(9, 17, 22))
    text = notifier.sent[0].text
    section = text.split("이번 주 안에 할 일\n", 1)[1].split("\n\n", 1)[0]
    assert "모레 시험" in section and "다음 주 과제" in section
    assert "내일 발표" not in section and "먼 마감" not in text


async def test_evening_calendar_shows_today_and_tomorrow():
    from types import SimpleNamespace

    from app.google.calendar import CalendarEvent
    from app.scheduler.briefing import CalendarBriefing

    def event(title, start):
        return CalendarEvent(id=title, title=title, start=start, end=start + timedelta(hours=1))

    class Accounts:
        ready, multiple = True, False

        async def events(self, start, end):
            day = to_kst(start).day
            found = [event("오늘 회의", kst(9, 17, 14))] if day == 17 else [event("내일 수업", kst(9, 18, 10))]
            return SimpleNamespace(events=found, failed=[])

    items = await CalendarBriefing(Accounts()).briefing_items(BriefingKind.EVENING, kst(9, 17, 22))
    sections = {item.section: item.text for item in items}
    assert "내일 수업" in sections["내일 일정"] and "오늘 회의" in sections["오늘 지난 일정"]
