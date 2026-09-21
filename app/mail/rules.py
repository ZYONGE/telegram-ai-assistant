"""메일 규칙 엔진.

- 모델이 임의로 스팸을 판정하지 않는다 (CLAUDE.md 6절). 판단은 전부 코드가 한다.
- 먼저 **사용자가 등록한 규칙**을 본다 (발신자·도메인·키워드).
- 맞는 규칙이 없으면 사용자가 지시 파일에 적은 분류표(2026-09-21)를 코드로 옮긴 `AutoPolicy`를 쓴다.
  개인 주소 → 바로 알림 · 학교 주소 → 바로 알림 + 중요 표시 · 결제 확인 → 영수증 보관함 ·
  보안 알림·광고 → 휴지통 · 그 밖의 기업 주소 → 스팸함 (설정으로 끌 수 있다).
- 휴지통·스팸함 이동은 규칙 엔진만 하고, 보호 목록(학교·개인 주소·교수님·지원 기업)은 어떤 경우에도 뺀다.
- 메일 제목·본문은 외부에서 온 데이터다. 매칭에만 쓰고 지시로 해석하지 않는다 (절대 규칙 8).
"""

from dataclasses import dataclass, field

from app.core.interfaces import MailAction, MailMessage, RuleResult

OTHER = "other"
# 옮기거나 지우는 동작. 보호 목록이면 뺀다.
REMOVING = frozenset({MailAction.TRASH, MailAction.SPAM})


@dataclass(frozen=True, slots=True)
class MailKind:
    key: str
    label: str
    actions: tuple[MailAction, ...]
    # 즉시 알림으로 올릴지 (알림 게이트의 일일 상한을 함께 받는다)
    urgent: bool = False
    # 이 유형으로 등록된 발신자는 휴지통 이동에서 제외한다
    protected: bool = False


KINDS: dict[str, MailKind] = {
    # 결제 확인은 알리지 않고 영수증 보관함에 모은다 (사용자 지시: 먼저 말 걸 일은 사람이 보낸 메일뿐)
    "payment": MailKind("payment", "결제·영수증", (MailAction.EVENING_CLEANUP, MailAction.FILE_RECEIPT)),
    "professor": MailKind(
        "professor", "교수님·학과", (MailAction.NOTIFY, MailAction.TRACK_REPLY), urgent=True, protected=True
    ),
    "company": MailKind(
        "company", "지원 기업", (MailAction.NOTIFY, MailAction.SUGGEST_SCHEDULE), urgent=True, protected=True
    ),
    "ad": MailKind("ad", "광고", (MailAction.EVENING_CLEANUP, MailAction.TRASH)),
    # 아래는 등록하지 않아도 자동 분류가 붙이는 유형
    "person": MailKind("person", "개인 메일", (MailAction.NOTIFY,), urgent=True, protected=True),
    "school": MailKind(
        "school", "학교 메일", (MailAction.NOTIFY, MailAction.MARK_IMPORTANT), urgent=True, protected=True
    ),
    "security": MailKind("security", "보안 알림", (MailAction.EVENING_CLEANUP, MailAction.TRASH)),
    "corporate": MailKind("corporate", "기업 메일", (MailAction.EVENING_CLEANUP, MailAction.SPAM)),
    OTHER: MailKind(OTHER, "그 외", (MailAction.MORNING_LIST,)),
}
# 사용자가 대화로 등록할 수 있는 유형 (나머지는 자동)
SELECTABLE = ("payment", "professor", "company", "ad")

# 사람이 쓰는 무료 메일. 여기서 온 메일은 개인이 보낸 것으로 본다.
PERSONAL_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "naver.com", "daum.net", "hanmail.net", "kakao.com", "nate.com",
        "outlook.com", "outlook.kr", "hotmail.com", "live.com", "icloud.com", "me.com", "yahoo.com",
        "yahoo.co.kr", "proton.me", "protonmail.com",
    }
)
# 무료 메일이라도 이런 주소는 사람이 아니다
AUTOMATED_MARKS = ("noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "notification", "alert")
# 결제 확인 (제목에서 찾는다)
RECEIPT_WORDS = (
    "영수증", "결제", "구매", "주문", "청구", "이용내역", "승인", "receipt", "invoice", "payment", "order",
)
# 보안 알림 (제목에서 찾는다)
SECURITY_WORDS = (
    "보안 알림", "보안 경고", "새 로그인", "새로운 로그인", "로그인 알림", "새 기기", "새로운 기기",
    "security alert", "new sign-in", "sign-in attempt", "new login",
)
# 어떤 분류도 하지 않고 받은편지함에 두는 것. 기다리고 있을 수 있는 메일이다.
KEEP_WORDS = (
    "인증번호", "인증 번호", "인증코드", "인증 코드", "확인 코드", "verification code", "otp", "passcode",
    "초대", "invitation",
)
PROMOTIONS = "CATEGORY_PROMOTIONS"


def _domain_in(domain: str, domains) -> bool:
    return bool(domain) and any(domain == item or domain.endswith(f".{item}") for item in domains)


@dataclass(frozen=True, slots=True)
class AutoPolicy:
    """등록한 규칙에 맞지 않는 메일을 나누는 기본 분류 (사용자 지시 파일 5절을 코드로 옮긴 것)."""

    enabled: bool = False
    school_domains: frozenset[str] = frozenset()
    # 기업 주소에서 온 나머지 메일을 스팸함으로. 끄면 받은편지함에 두고 아침 목록에만 넣는다.
    corporate_to_spam: bool = False

    def personal(self, message: MailMessage) -> bool:
        local = message.sender.partition("@")[0].lower()
        return message.sender_domain in PERSONAL_DOMAINS and not any(mark in local for mark in AUTOMATED_MARKS)

    def school(self, message: MailMessage) -> bool:
        return _domain_in(message.sender_domain, self.school_domains)

    def classify(self, message: MailMessage) -> tuple[str, str]:
        """(유형, 까닭)."""
        subject = message.subject.lower()
        if any(word in subject for word in KEEP_WORDS):
            return OTHER, "인증번호·초대 메일은 받은편지함에 둠"
        if self.school(message):
            return "school", "학교 주소"
        if self.personal(message):
            return "person", "개인 주소"
        if any(word in subject for word in RECEIPT_WORDS):
            return "payment", "결제 확인 메일"
        if any(word in subject for word in SECURITY_WORDS):
            return "security", "보안 알림 메일"
        if PROMOTIONS in message.labels:
            return "ad", "Gmail이 프로모션으로 분류"
        if self.corporate_to_spam:
            return "corporate", "기업 주소"
        return OTHER, "등록된 유형에 맞지 않음"


@dataclass(frozen=True, slots=True)
class MailRule:
    """사용자가 등록한 메일 유형 하나."""

    id: int
    name: str
    kind: str
    senders: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    # 특정 계정에만 적용 (비우면 모든 계정)
    account: str = ""
    enabled: bool = True

    @property
    def spec(self) -> MailKind:
        return KINDS.get(self.kind, KINDS[OTHER])

    def matches_account(self, account: str) -> bool:
        return not self.account or self.account == account

    def evaluate(self, message: MailMessage) -> RuleResult:
        sender = message.sender.lower()
        domain = message.sender_domain
        haystack = f"{message.subject} {message.snippet}".lower()

        if sender and sender in self.senders:
            return RuleResult(True, self.spec.actions, f"{self.name}: 등록한 발신자")
        if _domain_in(domain, self.domains):
            return RuleResult(True, self.spec.actions, f"{self.name}: 등록한 도메인")
        for keyword in self.keywords:
            if keyword and keyword.lower() in haystack:
                return RuleResult(True, self.spec.actions, f"{self.name}: 키워드 '{keyword}'")
        return RuleResult(False)


@dataclass(frozen=True, slots=True)
class Verdict:
    """메일 한 통에 대한 판단."""

    kind: str
    actions: tuple[MailAction, ...]
    reason: str
    rule: MailRule | None = None
    # 보호 목록이라 휴지통·스팸함 이동을 뺐는지
    trash_blocked: bool = False

    @property
    def label(self) -> str:
        return KINDS.get(self.kind, KINDS[OTHER]).label

    @property
    def urgent(self) -> bool:
        return KINDS.get(self.kind, KINDS[OTHER]).urgent


@dataclass(frozen=True, slots=True)
class RuleEngine:
    rules: tuple[MailRule, ...] = ()
    # 설정에서 더 넣은 보호 도메인 (학교 도메인 등)
    protected_domains: frozenset[str] = field(default_factory=frozenset)
    auto: AutoPolicy = field(default_factory=AutoPolicy)

    def protected(self, message: MailMessage) -> bool:
        """휴지통·스팸함으로 보내면 안 되는 발신자인지.

        설정 도메인, 학교 주소, 개인 주소(사용자 지시), 보호 유형(교수님·지원 기업)으로 등록한 규칙을 함께 본다.
        """
        domain, sender = message.sender_domain, message.sender.lower()
        if _domain_in(domain, self.protected_domains) or self.auto.school(message):
            return True
        if self.auto.enabled and self.auto.personal(message):
            return True
        for rule in self.rules:
            if not rule.spec.protected or not rule.enabled:
                continue
            if sender in rule.senders or _domain_in(domain, rule.domains):
                return True
        return False

    def classify(self, message: MailMessage, account: str = "") -> Verdict:
        """먼저 맞는 등록 규칙 하나를 쓴다. 없으면 자동 분류, 그것도 꺼져 있으면 '그 외'."""
        for rule in self.rules:
            if not rule.enabled or not rule.matches_account(account):
                continue
            result = rule.evaluate(message)
            if result.matched:
                return self._verdict(rule.kind, result.actions, result.reason, message, rule)
        if self.auto.enabled:
            kind, reason = self.auto.classify(message)
            return self._verdict(kind, KINDS[kind].actions, f"자동 분류: {reason}", message, None)
        other = KINDS[OTHER]
        return self._verdict(OTHER, other.actions, "등록된 유형에 맞지 않음", message, None)

    def _verdict(self, kind: str, actions, reason: str, message: MailMessage, rule: MailRule | None) -> Verdict:
        blocked = bool(REMOVING & set(actions)) and self.protected(message)
        if blocked:
            actions = tuple(action for action in actions if action not in REMOVING)
            reason = f"{reason} (보호 목록이라 옮기지 않음)"
            if kind == "corporate":
                # 스팸함 말고는 할 일이 없는 유형이다. 옮기지 않으면 그냥 새 메일이다.
                kind, actions = OTHER, KINDS[OTHER].actions
        return Verdict(kind=kind, actions=tuple(actions), reason=reason, rule=rule, trash_blocked=blocked)
