import pytest

from app.collectors.mail import MailCollector, alert_text
from app.core.events import EventKind, EventSource
from app.mail.rules import RuleEngine
from app.storage.mail import MailCleanupLog, MailRuleRepository, MailStateStore, WaitingReplyStore
from tests.conftest import kst
from tests.mail.fakes import FakeAccount, FakeAccounts, FakeGmail, api_error, message

NOW = kst(9, 18, 10)


@pytest.fixture
def stores(db):
    return {
        "rules": MailRuleRepository(db),
        "state": MailStateStore(db),
        "cleanup": MailCleanupLog(db),
        "waiting": WaitingReplyStore(db),
    }


def collector(accounts, stores, protected=frozenset(), now=NOW):
    return MailCollector(
        accounts, stores["rules"], stores["state"], stores["cleanup"], stores["waiting"], protected, lambda: now
    )


async def seed_rules(stores):
    now = kst(9, 17, 10)
    await stores["rules"].add("교수님", "professor", now, domains=("example.ac.kr",))
    await stores["rules"].add("카드 결제", "payment", now, senders=("card@bank.example",))
    await stores["rules"].add("쇼핑 광고", "ad", now, keywords=("특가",))


# --- 첫 실행과 커서 ---


async def test_first_run_only_remembers_the_cursor(stores):
    gmail = FakeGmail([message()], history_id="500")
    accounts = FakeAccounts(FakeAccount("개인", gmail, default=True))

    events = await collector(accounts, stores).collect()

    assert events == []
    assert await stores["state"].history_id("개인") == "500"
    assert gmail.trashed == []


async def test_second_run_processes_new_mail_and_moves_the_cursor(stores):
    await seed_rules(stores)
    gmail = FakeGmail([message()], history_id="510")
    accounts = FakeAccounts(FakeAccount("개인", gmail, default=True))
    await stores["state"].save_history_id("개인", "500", NOW)

    events = await collector(accounts, stores).collect()

    assert [event.kind for event in events] == [EventKind.MAIL]
    assert events[0].source == EventSource.GMAIL and events[0].urgent is True
    assert events[0].ref_id == "gmail:개인:msg-1"
    assert await stores["state"].history_id("개인") == "510"


async def test_same_mail_is_not_processed_twice(stores):
    await seed_rules(stores)
    gmail = FakeGmail([message()])
    accounts = FakeAccounts(FakeAccount("개인", gmail, default=True))
    await stores["state"].save_history_id("개인", "1", NOW)

    first = await collector(accounts, stores).collect()
    second = await collector(accounts, stores).collect()
    assert len(first) == 1 and second == []


# --- 유형별 동작 ---


async def test_professor_mail_is_notified_and_tracked(stores):
    await seed_rules(stores)
    gmail = FakeGmail([message(sender="prof@example.ac.kr", subject="면담 일정")])
    accounts = FakeAccounts(FakeAccount("학교", gmail, default=True))
    await stores["state"].save_history_id("학교", "1", NOW)

    events = await collector(accounts, stores).collect()

    assert "교수님·학과: 홍길동" in events[0].title
    assert "답변 대기로 등록했습니다." in events[0].body
    waiting = await stores["waiting"].open_items()
    assert [(item.account, item.thread_id, item.sender) for item in waiting] == [
        ("학교", "thread-1", "prof@example.ac.kr")
    ]
    assert waiting[0].due_at == kst(9, 21, 10)
    assert gmail.trashed == []


async def test_payment_mail_is_filed_as_a_receipt_without_an_alert(stores):
    """결제 확인은 알리지 않고 영수증 보관함에 모은다 (사용자 지시 2026-09-21)."""
    await seed_rules(stores)
    gmail = FakeGmail([message(message_id="m-2", sender="card@bank.example", subject="9월 결제 안내")])
    accounts = FakeAccounts(FakeAccount("개인", gmail, default=True))
    await stores["state"].save_history_id("개인", "1", NOW)

    events = await collector(accounts, stores).collect()

    assert events == [] and gmail.trashed == []
    assert gmail.filed == [("m-2", "Label_1")] and gmail.created_labels == {"Receipt": "Label_1"}
    cleaned = await stores["cleanup"].since(kst(9, 18, 0))
    assert [(record.message_id, record.action, record.label_id) for record in cleaned] == [("m-2", "file", "Label_1")]


async def test_ad_mail_is_cleaned_without_an_alert(stores):
    await seed_rules(stores)
    gmail = FakeGmail([message(message_id="m-3", sender="shop@shop.example", subject="가을 특가")])
    accounts = FakeAccounts(FakeAccount("개인", gmail, default=True))
    await stores["state"].save_history_id("개인", "1", NOW)

    events = await collector(accounts, stores).collect()

    assert events == []  # 저녁 브리핑 정리 내역에만 들어간다
    assert gmail.trashed == ["m-3"]


async def test_other_mail_waits_for_the_morning_list(stores):
    await seed_rules(stores)
    gmail = FakeGmail([message(message_id="m-4", sender="friend@example.com", subject="주말에 뭐해")])
    accounts = FakeAccounts(FakeAccount("개인", gmail, default=True))
    await stores["state"].save_history_id("개인", "1", NOW)

    events = await collector(accounts, stores).collect()

    assert events == [] and gmail.trashed == []
    assert await stores["state"].unbriefed("other") == [("개인", "주말에 뭐해")]


async def test_protected_domain_is_never_trashed(stores):
    await stores["rules"].add("쇼핑 광고", "ad", kst(9, 17, 10), keywords=("특가",))
    gmail = FakeGmail([message(message_id="m-5", sender="notice@example.ac.kr", subject="학과 특가 행사")])
    accounts = FakeAccounts(FakeAccount("학교", gmail, default=True))
    await stores["state"].save_history_id("학교", "1", NOW)

    events = await collector(accounts, stores, protected=frozenset({"example.ac.kr"})).collect()

    assert gmail.trashed == []
    assert events == []  # 광고 유형이라 알림은 없지만, 휴지통 이동도 하지 않는다
    assert await stores["cleanup"].since(kst(9, 18, 0)) == []


# --- 여러 계정과 실패 처리 ---


async def test_every_connected_account_is_checked(stores):
    await seed_rules(stores)
    personal = FakeGmail([message(message_id="p-1", sender="card@bank.example", subject="결제")])
    school = FakeGmail([message(message_id="s-1", sender="prof@example.ac.kr", subject="면담")])
    accounts = FakeAccounts(FakeAccount("개인", personal, default=True), FakeAccount("학교", school))
    await stores["state"].save_history_id("개인", "1", NOW)
    await stores["state"].save_history_id("학교", "1", NOW)

    events = await collector(accounts, stores).collect()

    assert len(events) == 1
    # 계정이 둘 이상이면 알림에 계정 이름을 붙인다
    assert events[0].title.startswith("[학교]")
    assert [item for item, _label in personal.filed] == ["p-1"] and school.filed == []


async def test_one_failing_account_is_reported_but_others_continue(stores):
    await seed_rules(stores)
    broken = FakeGmail()
    broken.fail_with = api_error("Gmail 요청 오류 (HTTP 500).")
    good = FakeGmail([message(message_id="s-1", sender="prof@example.ac.kr")])
    accounts = FakeAccounts(FakeAccount("개인", broken, default=True), FakeAccount("학교", good))
    await stores["state"].save_history_id("개인", "1", NOW)
    await stores["state"].save_history_id("학교", "1", NOW)

    events = await collector(accounts, stores).collect()

    kinds = [event.kind for event in events]
    assert EventKind.COLLECTOR_FAILED in kinds and EventKind.MAIL in kinds


async def test_nothing_happens_without_a_connected_account(stores):
    accounts = FakeAccounts(FakeAccount("개인", FakeGmail(), connected=False))
    assert await collector(accounts, stores).collect() == []


# --- 답변 대기 자동 해제 ---


async def test_reply_in_the_thread_resolves_the_waiting_item(stores):
    gmail = FakeGmail()
    accounts = FakeAccounts(FakeAccount("학교", gmail, default=True))
    await stores["state"].save_history_id("학교", "1", NOW)
    await stores["waiting"].add("학교", "thread-1", "m-1", "면담 일정", "prof@example.ac.kr", kst(9, 17, 9))

    assert len(await stores["waiting"].open_items()) == 1
    gmail.replied_threads.add("thread-1")
    await collector(accounts, stores).collect()
    assert await stores["waiting"].open_items() == []


# --- 알림 문구 ---


def test_alert_text_marks_outside_content_and_actions():
    from app.mail.rules import RuleEngine as Engine

    verdict = Engine((), frozenset()).classify(message())
    title, body = alert_text(message(), verdict, "개인", show_account=True)
    assert title.startswith("[개인] 그 외: 홍길동 <prof@example.ac.kr>")
    assert body.splitlines()[0] == "제목: 면담 일정"


def test_engine_without_rules_sends_everything_to_the_morning_list():
    verdict = RuleEngine(()).classify(message())
    assert verdict.kind == "other"
