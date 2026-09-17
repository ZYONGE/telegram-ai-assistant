"""세로 한 줄: 가짜 수집기 → Event → 할 일 등록 → 알림 게이트 → 발송."""

from datetime import UTC, datetime, timedelta

from app.collectors.fake import FakeCollector, sample_events
from app.core.events import EventKind
from app.core.interfaces import GateAction
from app.scheduler.ingest import Ingestor
from tests.conftest import kst, make_event


async def test_fake_collector_flows_to_todo_and_telegram(todos, dispatcher, notifier):
    now = kst(9, 17, 14)
    ingestor = Ingestor(todos, dispatcher)

    decisions = await ingestor.run_collector(FakeCollector(sample_events(now)), now)

    assert [d.action for d in decisions] == [GateAction.SEND_NOW, GateAction.BATCH]
    assert [m.text.splitlines()[0] for m in notifier.sent] == ["[테스트] 자료구조 휴강 안내"]
    [todo] = await todos.list_open()
    assert todo.title == "[테스트] 알고리즘 과제 2 제출"
    assert todo.due_at == now + timedelta(days=3)


async def test_repeated_collection_does_not_duplicate_todos_or_alerts(todos, dispatcher, notifier):
    now = kst(9, 17, 14)
    ingestor = Ingestor(todos, dispatcher)
    collector = FakeCollector(sample_events(now))

    await ingestor.run_collector(collector, now)
    second = await ingestor.run_collector(collector, now + timedelta(hours=1))

    assert [d.action for d in second] == [GateAction.DROP, GateAction.DROP]
    assert len(await todos.list_open()) == 1
    assert len(notifier.sent) == 1


async def test_only_deadline_events_become_todos(todos, dispatcher):
    due = datetime(2026, 9, 20, 14, 59, tzinfo=UTC)
    events = [
        make_event("notice"),
        make_event("deadline", kind=EventKind.DEADLINE, title="보고서", due_at=due),
    ]
    await Ingestor(todos, dispatcher).ingest(events, kst(9, 17, 14))
    assert [t.ref_id for t in await todos.list_open()] == ["deadline"]


async def test_crashing_collector_is_reported_as_collector_failed(todos, dispatcher, log):
    class BrokenCollector:
        name = "eclass"

        async def collect(self):
            raise RuntimeError("password=secret")

    decisions = await Ingestor(todos, dispatcher).run_collector(BrokenCollector(), kst(9, 17, 14))

    assert [d.action for d in decisions] == [GateAction.BATCH]
    record = await log.get("eclass:collector_failed:unexpected_error")
    assert record.event.kind == EventKind.COLLECTOR_FAILED
    assert "secret" not in record.event.body


async def test_one_failing_event_does_not_stop_the_rest(todos, dispatcher, notifier):
    notifier.fail = True
    events = [make_event("a", urgent=True), make_event("b")]
    decisions = await Ingestor(todos, dispatcher).ingest(events, kst(9, 17, 14))
    assert [d.action for d in decisions] == [GateAction.BATCH]
