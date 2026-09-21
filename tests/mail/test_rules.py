import pytest

from app.core.interfaces import MailAction
from app.mail.rules import KINDS, OTHER, MailRule, RuleEngine
from tests.mail.fakes import message

PAYMENT = MailRule(1, "카드 결제", "payment", senders=("noreply@card.example.com",), keywords=("결제",))
PROFESSOR = MailRule(2, "교수님", "professor", domains=("example.ac.kr",))
COMPANY = MailRule(3, "지원 기업", "company", domains=("berlin-corp.example",))
AD = MailRule(4, "쇼핑 광고", "ad", keywords=("특가", "세일"))
ENGINE = RuleEngine((PAYMENT, PROFESSOR, COMPANY, AD))


def test_sender_domain_and_keyword_matching():
    assert PAYMENT.evaluate(message(sender="noreply@card.example.com", subject="이용 안내")).matched
    assert PAYMENT.evaluate(message(sender="other@example.com", subject="9월 결제 내역")).matched
    assert PROFESSOR.evaluate(message(sender="prof@example.ac.kr")).matched
    # 하위 도메인도 같은 학교로 본다
    assert PROFESSOR.evaluate(message(sender="office@cs.example.ac.kr")).matched
    assert PROFESSOR.evaluate(message(sender="someone@example.ac.kr.evil.com")).matched is False
    assert AD.evaluate(message(subject="가을 특가 세일")).matched


def test_keyword_matching_uses_subject_and_snippet():
    assert AD.evaluate(message(subject="안내", snippet="오늘만 특가")).matched
    assert AD.evaluate(message(subject="안내", snippet="평범한 내용")).matched is False


def test_disabled_rule_is_skipped():
    engine = RuleEngine((MailRule(1, "광고", "ad", keywords=("특가",), enabled=False),))
    assert engine.classify(message(subject="특가")).kind == OTHER


def test_account_scoped_rule_applies_only_there():
    rule = MailRule(1, "학교 메일", "professor", domains=("example.ac.kr",), account="학교")
    engine = RuleEngine((rule,))
    assert engine.classify(message(sender="prof@example.ac.kr"), account="학교").kind == "professor"
    assert engine.classify(message(sender="prof@example.ac.kr"), account="개인").kind == OTHER


def test_first_matching_rule_wins():
    verdict = ENGINE.classify(message(sender="prof@example.ac.kr", subject="특가 세미나"))
    assert verdict.kind == "professor" and "등록한 도메인" in verdict.reason


def test_unmatched_mail_goes_to_the_morning_list():
    verdict = ENGINE.classify(message(sender="friend@example.com", subject="안녕"))
    assert verdict.kind == OTHER
    assert verdict.actions == (MailAction.MORNING_LIST,) and verdict.urgent is False


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("payment", (MailAction.EVENING_CLEANUP, MailAction.FILE_RECEIPT)),
        ("professor", (MailAction.NOTIFY, MailAction.TRACK_REPLY)),
        ("company", (MailAction.NOTIFY, MailAction.SUGGEST_SCHEDULE)),
        ("ad", (MailAction.EVENING_CLEANUP, MailAction.TRASH)),
    ],
)
def test_actions_follow_the_type_table(kind, expected):
    assert KINDS[kind].actions == expected


def test_protected_senders_are_never_trashed():
    # 광고 키워드가 맞아도 학교 도메인이면 휴지통으로 보내지 않는다
    verdict = ENGINE.classify(message(sender="office@example.ac.kr", subject="학과 특가 행사"))
    assert verdict.kind == "professor"

    ad_from_school = RuleEngine((AD, PROFESSOR)).classify(message(sender="office@example.ac.kr", subject="특가"))
    assert ad_from_school.kind == "ad"
    assert MailAction.TRASH not in ad_from_school.actions
    assert ad_from_school.trash_blocked is True and "보호 목록" in ad_from_school.reason


def test_configured_protected_domains_also_block_trashing():
    engine = RuleEngine((AD,), frozenset({"example.ac.kr"}))
    verdict = engine.classify(message(sender="notice@example.ac.kr", subject="특가"))
    assert MailAction.TRASH not in verdict.actions and verdict.trash_blocked is True


def test_company_mail_is_protected_too():
    verdict = RuleEngine((AD, COMPANY)).classify(message(sender="jobs@berlin-corp.example", subject="특가"))
    assert verdict.trash_blocked is True


def test_ordinary_ad_is_trashed():
    verdict = ENGINE.classify(message(sender="shop@shop.example.com", subject="가을 세일"))
    assert verdict.kind == "ad" and MailAction.TRASH in verdict.actions
    assert verdict.trash_blocked is False and verdict.urgent is False
