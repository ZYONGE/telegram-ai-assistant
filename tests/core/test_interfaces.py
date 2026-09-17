"""인터페이스 계약 테스트: 가짜 구현이 Protocol을 만족하는지, 값 객체 검증이 동작하는지 확인한다."""

from datetime import UTC, datetime

import pytest

from app.core.events import Event, EventKind, EventSource, collector_failed
from app.core.interfaces import (
    NO_MATCH,
    BriefingItem,
    BriefingKind,
    BriefingProvider,
    Collector,
    Confirmation,
    GateAction,
    GateDecision,
    MailAction,
    MailMessage,
    MemoryItem,
    MemoryStore,
    NotificationGate,
    Notifier,
    OutgoingMessage,
    Rule,
    RuleResult,
    Tool,
    ToolResult,
    ToolSpec,
)

NOW = datetime(2026, 9, 17, 14, 0, tzinfo=UTC)


class FakeCollector:
    name = "fake"

    async def collect(self) -> list[Event]:
        return [collector_failed("fake", "network")]


class FakeRule:
    name = "결제·영수증"

    async def evaluate(self, message: MailMessage) -> RuleResult:
        if message.sender_domain == "pay.example.com":
            return RuleResult(True, (MailAction.NOTIFY, MailAction.TRASH), "발신 도메인 일치")
        return NO_MATCH


class FakeMemory:
    def __init__(self):
        self.items: dict[str, MemoryItem] = {}

    async def read(self) -> list[MemoryItem]:
        return list(self.items.values())

    async def add(self, text: str) -> MemoryItem:
        item = MemoryItem(str(len(self.items) + 1), text, NOW)
        self.items[item.item_id] = item
        return item

    async def delete(self, item_id: str) -> bool:
        return self.items.pop(item_id, None) is not None


class FakeGate:
    async def decide(self, event: Event, now: datetime) -> GateDecision:
        return GateDecision(GateAction.SEND_NOW if event.urgent else GateAction.BATCH, "테스트")


class FakeBriefing:
    name = "todo"

    async def briefing_items(self, kind: BriefingKind, now: datetime) -> list[BriefingItem]:
        return [BriefingItem("할 일", "보고서 제출", priority=1)]


class FakeNotifier:
    def __init__(self):
        self.sent: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> None:
        self.sent.append(message)


class FakeTool:
    spec = ToolSpec("list_todos", "할 일 목록을 조회합니다.", {"type": "object"}, Confirmation.IMMEDIATE)

    async def run(self, args) -> ToolResult:
        return ToolResult("· 보고서 제출")


@pytest.mark.parametrize(
    ("impl", "protocol"),
    [
        (FakeCollector(), Collector),
        (FakeRule(), Rule),
        (FakeMemory(), MemoryStore),
        (FakeGate(), NotificationGate),
        (FakeBriefing(), BriefingProvider),
        (FakeNotifier(), Notifier),
        (FakeTool(), Tool),
    ],
)
def test_fakes_satisfy_protocols(impl, protocol):
    assert isinstance(impl, protocol)


def test_object_missing_method_does_not_satisfy_protocol():
    class NotACollector:
        name = "broken"

    assert not isinstance(NotACollector(), Collector)


def make_mail(sender: str) -> MailMessage:
    return MailMessage(message_id="m1", thread_id="t1", sender=sender, received_at=NOW)


def test_mail_sender_domain_is_lowercase():
    assert make_mail("Receipt@Pay.Example.COM").sender_domain == "pay.example.com"


def test_mail_rejects_naive_received_at():
    with pytest.raises(ValueError):
        MailMessage(message_id="m1", thread_id="t1", sender="a@b.c", received_at=datetime(2026, 9, 17))


async def test_rule_returns_actions_only_when_matched():
    rule = FakeRule()
    matched = await rule.evaluate(make_mail("receipt@pay.example.com"))
    other = await rule.evaluate(make_mail("prof@example.ac.kr"))

    assert matched.matched and MailAction.TRASH in matched.actions
    assert other is NO_MATCH and other.actions == ()


def test_unmatched_rule_result_cannot_carry_actions():
    with pytest.raises(ValueError):
        RuleResult(False, (MailAction.TRASH,))


def test_hold_decision_requires_aware_release_time():
    with pytest.raises(ValueError):
        GateDecision(GateAction.HOLD, "조용한 시간")
    with pytest.raises(ValueError):
        GateDecision(GateAction.HOLD, "조용한 시간", release_at=datetime(2026, 9, 18, 6, 30))
    assert GateDecision(GateAction.HOLD, "조용한 시간", release_at=NOW).release_at == NOW


def test_release_time_only_allowed_for_hold():
    with pytest.raises(ValueError):
        GateDecision(GateAction.SEND_NOW, "급함", release_at=NOW)


async def test_memory_store_round_trip():
    store = FakeMemory()
    item = await store.add("월요일 오전에는 수업이 없음")
    assert [i.text for i in await store.read()] == ["월요일 오전에는 수업이 없음"]
    assert await store.delete(item.item_id) is True
    assert await store.delete(item.item_id) is False


async def test_gate_notifier_briefing_and_tool_contracts():
    event = Event(source=EventSource.ECLASS, kind=EventKind.NOTICE, title="휴강", ref_id="n1", urgent=True)
    decision = await FakeGate().decide(event, NOW)
    notifier = FakeNotifier()
    if decision.action is GateAction.SEND_NOW:
        await notifier.send(OutgoingMessage(event.title))

    assert [m.text for m in notifier.sent] == ["휴강"]
    assert (await FakeBriefing().briefing_items(BriefingKind.MORNING, NOW))[0].section == "할 일"
    assert (await FakeTool().run({})).is_error is False
