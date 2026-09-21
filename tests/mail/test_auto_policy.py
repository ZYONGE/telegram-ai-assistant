"""등록한 규칙에 맞지 않는 메일의 자동 분류 (사용자 지시 파일 5절, 2026-09-21).

개인 주소 → 바로 알림 · 학교 주소 → 알림 + 중요 표시 · 결제 확인 → 영수증 보관함 ·
보안 알림·광고 → 휴지통 · 그 밖의 기업 주소 → 스팸함. 보호 목록은 어떤 경우에도 옮기지 않는다.
"""

import pytest

from app.collectors.mail import MailCollector
from app.core.interfaces import BriefingKind, MailAction
from app.mail.rules import OTHER, AutoPolicy, MailRule, RuleEngine
from app.mail.service import MailService
from app.scheduler.briefing import MailBriefing
from app.storage.mail import MailCleanupLog, MailRuleRepository, MailStateStore, WaitingReplyStore
from tests.conftest import kst
from tests.mail.fakes import FakeAccount, FakeAccounts, FakeGmail, message

NOW = kst(9, 18, 10)
POLICY = AutoPolicy(enabled=True, school_domains=frozenset({"school.example.ac.kr"}), corporate_to_spam=True)
ENGINE = RuleEngine(auto=POLICY)


@pytest.mark.parametrize(
    ("sender", "subject", "labels", "kind"),
    [
        ("friend@gmail.com", "주말에 볼래?", (), "person"),
        ("friend@naver.com", "사진 보냄", (), "person"),
        ("prof@school.example.ac.kr", "면담 안내", (), "school"),
        ("office@dept.school.example.ac.kr", "학과 공지", (), "school"),
        ("noreply@gmail.com", "알림", (), "corporate"),  # 무료 메일이라도 사람이 아니다
        ("billing@shop.example.com", "주문하신 상품의 결제가 완료되었습니다", (), "payment"),
        ("no-reply@accounts.example.com", "보안 알림: 새 기기에서 로그인", (), "security"),
        ("news@brand.example.com", "가을 신상품", ("CATEGORY_PROMOTIONS",), "ad"),
        ("hello@startup.example.com", "뉴스레터 9월호", (), "corporate"),
        ("auth@service.example.com", "[서비스] 인증번호 안내", (), OTHER),  # 기다리는 메일일 수 있다
        ("calendar@service.example.com", "초대: 팀 회의", (), OTHER),
    ],
)
def test_the_users_table_is_applied(sender, subject, labels, kind):
    verdict = ENGINE.classify(message(sender=sender, subject=subject, labels=frozenset(labels)))
    assert verdict.kind == kind


def test_registered_rules_come_first():
    rule = MailRule(1, "스터디", "professor", senders=("friend@gmail.com",))
    assert RuleEngine((rule,), auto=POLICY).classify(message(sender="friend@gmail.com")).kind == "professor"


def test_corporate_mail_stays_when_spam_is_off():
    engine = RuleEngine(auto=AutoPolicy(enabled=True, corporate_to_spam=False))
    assert engine.classify(message(sender="hello@startup.example.com", subject="뉴스레터")).kind == OTHER


def test_without_the_policy_everything_unmatched_goes_to_the_morning_list():
    assert RuleEngine().classify(message(sender="friend@gmail.com")).kind == OTHER


def test_people_and_school_are_never_trashed_or_spammed():
    """개인·학교 주소는 보호 목록이다 (지시 파일). 광고 규칙에 걸려도 옮기지 않는다."""
    ad = MailRule(1, "광고", "ad", keywords=("특가",))
    engine = RuleEngine((ad,), auto=POLICY)
    for sender in ("friend@gmail.com", "office@school.example.ac.kr"):
        verdict = engine.classify(message(sender=sender, subject="특가 안내"))
        assert MailAction.TRASH not in verdict.actions and verdict.trash_blocked


def test_an_applied_company_is_never_spammed():
    company = MailRule(1, "지원한 회사", "company", domains=("dream.example.com",))
    engine = RuleEngine((company,), auto=POLICY)
    # 지원 기업 규칙이 보호하는 도메인이라, 규칙에 안 걸린 다른 주소여도 스팸으로 보내지 않는다
    verdict = engine.classify(message(sender="hr@team.dream.example.com", subject="안내"))
    assert verdict.kind in ("company", OTHER) and MailAction.SPAM not in verdict.actions


def test_school_mail_is_marked_important_and_notified():
    verdict = ENGINE.classify(message(sender="prof@school.example.ac.kr"))
    assert verdict.actions == (MailAction.NOTIFY, MailAction.MARK_IMPORTANT) and verdict.urgent


# --- 수집기 ---


@pytest.fixture
def stores(db):
    return {
        "rules": MailRuleRepository(db),
        "state": MailStateStore(db),
        "cleanup": MailCleanupLog(db),
        "waiting": WaitingReplyStore(db),
    }


async def collect(stores, gmail: FakeGmail):
    accounts = FakeAccounts(FakeAccount("개인", gmail, default=True))
    await stores["state"].save_history_id("개인", "1", NOW)
    collector = MailCollector(
        accounts, stores["rules"], stores["state"], stores["cleanup"], stores["waiting"],
        clock=lambda: NOW, auto=POLICY,
    )
    return await collector.collect()


async def test_each_kind_goes_to_its_place(stores):
    gmail = FakeGmail(
        [
            message("m-person", sender="friend@gmail.com", subject="안녕", thread_id="t1"),
            message("m-school", sender="prof@school.example.ac.kr", subject="면담", thread_id="t2"),
            message("m-receipt", sender="billing@shop.example.com", subject="결제 완료", thread_id="t3"),
            message("m-alert", sender="no-reply@id.example.com", subject="보안 알림", thread_id="t4"),
            message("m-corp", sender="hello@startup.example.com", subject="뉴스레터", thread_id="t5"),
        ]
    )
    events = await collect(stores, gmail)

    assert [event.ref_id for event in events] == ["gmail:개인:m-person", "gmail:개인:m-school"]
    assert "friend@gmail.com" in events[0].title  # 누가 보냈는지 짐작할 수 있게 주소도 넘긴다
    assert gmail.important == ["m-school"]
    assert gmail.filed == [("m-receipt", "Label_1")]
    assert gmail.trashed == ["m-alert"] and gmail.spammed == ["m-corp"]
    cleaned = await stores["cleanup"].since(kst(9, 18, 0))
    assert [(record.message_id, record.action) for record in cleaned] == [
        ("m-receipt", "file"),
        ("m-alert", "trash"),
        ("m-corp", "spam"),
    ]


async def test_a_conversation_the_user_replied_to_is_not_spammed(stores):
    gmail = FakeGmail([message("m-corp", sender="hr@company.example.com", subject="면접 일정", thread_id="t9")])
    gmail.replied_threads.add("t9")
    await collect(stores, gmail)
    assert gmail.spammed == []


# --- 되돌리기와 브리핑 ---


async def test_undo_takes_each_mail_back_from_where_it_went(db):
    gmail = FakeGmail()
    accounts = FakeAccounts(FakeAccount("개인", gmail, default=True))
    cleanup = MailCleanupLog(db)
    service = MailService(accounts, cleanup, WaitingReplyStore(db), MailStateStore(db), lambda: kst(9, 18, 22))
    await cleanup.record("개인", "m-1", "광고", "a@x.example", kst(9, 18, 9), "trash")
    await cleanup.record("개인", "m-2", "뉴스레터", "b@x.example", kst(9, 18, 9), "spam")
    await cleanup.record("개인", "m-3", "영수증", "c@x.example", kst(9, 18, 9), "file", "Label_1")

    items = await MailBriefing(service).briefing_items(BriefingKind.EVENING, kst(9, 18, 22))
    texts = [item.text for item in items if item.section == "메일 정리"]
    assert any("휴지통" in text for text in texts) and any("스팸함" in text for text in texts)
    assert any("영수증 보관함" in text for text in texts)

    assert await service.undo_cleanup(kst(9, 18, 0)) == (3, 0)
    assert gmail.untrashed == ["m-1"] and gmail.unspammed == ["m-2"] and gmail.unfiled == [("m-3", "Label_1")]


async def test_the_morning_list_has_mail_from_people_too(db):
    state = MailStateStore(db)
    service = MailService(FakeAccounts(), MailCleanupLog(db), WaitingReplyStore(db), state, lambda: NOW)
    await state.mark_seen("개인", "m-1", "person", "주말 약속", NOW)
    await state.mark_seen("학교", "m-2", "school", "면담 안내", NOW)
    await state.mark_seen("개인", "m-3", "corporate", "뉴스레터", NOW)
    assert [subject for _account, subject in await service.morning_list()] == ["주말 약속", "면담 안내"]
