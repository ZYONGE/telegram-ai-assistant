"""메일 규칙 엔진.

- 유형은 **사용자가 등록한 것만** 쓴다. 모델이 임의로 스팸을 판정하지 않는다 (CLAUDE.md 6절).
- 발신자·도메인·키워드 매칭은 전부 코드가 한다.
- 휴지통 이동은 규칙 엔진만 하고, 보호 목록(학교·지원 기업)은 어떤 규칙에서도 제외한다.
- 메일 제목·본문은 외부에서 온 데이터다. 매칭에만 쓰고 지시로 해석하지 않는다 (절대 규칙 8).
"""

from dataclasses import dataclass, field

from app.core.interfaces import MailAction, MailMessage, RuleResult

OTHER = "other"


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
    "payment": MailKind("payment", "결제·영수증", (MailAction.NOTIFY, MailAction.TRASH), urgent=True),
    "professor": MailKind(
        "professor", "교수님·학과", (MailAction.NOTIFY, MailAction.TRACK_REPLY), urgent=True, protected=True
    ),
    "company": MailKind(
        "company", "지원 기업", (MailAction.NOTIFY, MailAction.SUGGEST_SCHEDULE), urgent=True, protected=True
    ),
    "ad": MailKind("ad", "광고", (MailAction.EVENING_CLEANUP, MailAction.TRASH)),
    OTHER: MailKind(OTHER, "그 외", (MailAction.MORNING_LIST,)),
}
# 사용자가 고를 수 있는 유형 (그 외는 자동)
SELECTABLE = tuple(key for key in KINDS if key != OTHER)


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
        if domain and any(domain == item or domain.endswith(f".{item}") for item in self.domains):
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
    # 보호 목록이라 휴지통 이동을 뺐는지
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

    def protected(self, message: MailMessage) -> bool:
        """휴지통으로 보내면 안 되는 발신자인지. 보호 유형 규칙과 설정 도메인을 함께 본다."""
        domain, sender = message.sender_domain, message.sender.lower()
        if domain and any(domain == item or domain.endswith(f".{item}") for item in self.protected_domains):
            return True
        for rule in self.rules:
            if not rule.spec.protected or not rule.enabled:
                continue
            if sender in rule.senders:
                return True
            if domain and any(domain == item or domain.endswith(f".{item}") for item in rule.domains):
                return True
        return False

    def classify(self, message: MailMessage, account: str = "") -> Verdict:
        """먼저 맞는 규칙 하나를 쓴다. 맞는 규칙이 없으면 '그 외'로 본다."""
        for rule in self.rules:
            if not rule.enabled or not rule.matches_account(account):
                continue
            result = rule.evaluate(message)
            if result.matched:
                return self._verdict(rule.kind, result.actions, result.reason, message, rule)
        other = KINDS[OTHER]
        return self._verdict(OTHER, other.actions, "등록된 유형에 맞지 않음", message, None)

    def _verdict(self, kind: str, actions, reason: str, message: MailMessage, rule: MailRule | None) -> Verdict:
        blocked = MailAction.TRASH in actions and self.protected(message)
        if blocked:
            actions = tuple(action for action in actions if action is not MailAction.TRASH)
            reason = f"{reason} (보호 목록이라 휴지통 이동은 하지 않음)"
        return Verdict(kind=kind, actions=tuple(actions), reason=reason, rule=rule, trash_blocked=blocked)
