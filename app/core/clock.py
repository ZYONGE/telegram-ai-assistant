"""시간 규칙: 저장은 UTC, 판단·표시는 Asia/Seoul."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
_WEEKDAYS = "월화수목금토일"


def utc_now() -> datetime:
    return datetime.now(UTC)


def require_aware(value: datetime, name: str) -> datetime:
    """시간대 정보가 없는 datetime은 서버 시간대에 따라 해석이 달라지므로 거부한다."""
    if value.utcoffset() is None:
        raise ValueError(f"{name}에는 시간대 정보가 있어야 합니다: {value!r}")
    return value


def to_utc(value: datetime) -> datetime:
    return require_aware(value, "value").astimezone(UTC)


def to_kst(value: datetime) -> datetime:
    return require_aware(value, "value").astimezone(KST)


def format_kst(value: datetime) -> str:
    """사용자에게 보여 줄 시각. 예: 9월 18일(금) 23:59"""
    local = to_kst(value)
    return f"{local.month}월 {local.day}일({_WEEKDAYS[local.weekday()]}) {local:%H:%M}"
