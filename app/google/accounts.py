"""여러 Google 계정을 하나로 묶어 다룬다.

- 계정마다 토큰 파일이 따로 있고, OAuth 클라이언트 파일 하나를 함께 쓴다.
- 조회는 연결된 계정을 모두 돌아 합치고, 한 계정이 실패해도 나머지는 그대로 보여 준다.
- 계정 주소(이메일)는 저장하지 않는다. 사용자가 붙인 이름(label)으로만 구분한다.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

import httpx

from app.core.clock import utc_now
from app.core.config import GoogleSettings
from app.google.auth import GoogleApiError, GoogleAuth, GoogleAuthError
from app.google.calendar import CalendarClient, CalendarEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GoogleAccount:
    label: str
    auth: GoogleAuth
    calendar: CalendarClient
    default: bool = False

    @property
    def connected(self) -> bool:
        return self.auth.configured


@dataclass(frozen=True, slots=True)
class EventsResult:
    events: list[CalendarEvent]
    # 조회에 실패한 계정 이름. 있으면 사용자에게 한 줄로 알린다.
    failed: tuple[str, ...] = ()

    @property
    def warning(self) -> str:
        return f"({', '.join(self.failed)} 계정은 지금 확인하지 못했습니다)" if self.failed else ""


class GoogleAccounts:
    def __init__(
        self, settings: GoogleSettings, http: httpx.AsyncClient, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self.client_file = settings.client_file
        self.accounts = [
            GoogleAccount(
                label=account.label,
                auth=(auth := GoogleAuth(settings.client_file, account.token_file, http, clock=clock)),
                calendar=CalendarClient(auth, http, account.calendar_id),
                default=account.default,
            )
            for account in settings.accounts
        ]

    @property
    def connected(self) -> list[GoogleAccount]:
        return [account for account in self.accounts if account.connected]

    @property
    def ready(self) -> bool:
        return bool(self.connected)

    @property
    def multiple(self) -> bool:
        """연결된 계정이 둘 이상이면 표시에 계정 이름을 붙인다."""
        return len(self.connected) > 1

    @property
    def labels(self) -> list[str]:
        return [account.label for account in self.accounts]

    def find(self, label: str | None) -> GoogleAccount | None:
        if not label:
            return None
        wanted = label.strip().lower()
        for account in self.accounts:
            if account.label.lower() == wanted:
                return account
        return None

    def default_account(self) -> GoogleAccount | None:
        connected = self.connected
        for account in connected:
            if account.default:
                return account
        return connected[0] if connected else None

    def status(self) -> str:
        lines = [
            f"· {account.label}: {'연결됨' if account.connected else '연결 안 됨'}"
            + (" (기본)" if account.default else "")
            for account in self.accounts
        ]
        return "\n".join(lines)

    async def events(self, start: datetime, end: datetime, label: str | None = None) -> EventsResult:
        """연결된 계정들의 일정을 시작 시각 순으로 합친다."""
        targets = [found] if (found := self.find(label)) else self.connected
        events: list[CalendarEvent] = []
        failed: list[str] = []
        for account in targets:
            if not account.connected:
                failed.append(account.label)
                continue
            try:
                found_events = await account.calendar.list_events(start, end)
            except (GoogleApiError, GoogleAuthError) as exc:
                logger.info("%s 계정 일정 조회 실패: %s", account.label, exc)
                failed.append(account.label)
                continue
            events += [replace(event, account=account.label) for event in found_events]
        return EventsResult(sorted(events, key=lambda event: event.start), tuple(failed))
