from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import KST, require_aware, to_kst, to_utc, utc_now


def test_utc_now_is_aware_utc():
    assert utc_now().utcoffset() == timedelta(0)


def test_to_kst_adds_nine_hours():
    assert to_kst(datetime(2026, 9, 17, 14, 30, tzinfo=UTC)) == datetime(2026, 9, 17, 23, 30, tzinfo=KST)
    assert to_kst(datetime(2026, 9, 17, 14, 30, tzinfo=UTC)).hour == 23


def test_to_utc_round_trip():
    kst = datetime(2026, 9, 18, 6, 30, tzinfo=KST)
    assert to_utc(kst) == datetime(2026, 9, 17, 21, 30, tzinfo=UTC)


@pytest.mark.parametrize("func", [to_kst, to_utc])
def test_naive_datetime_is_rejected(func):
    with pytest.raises(ValueError):
        func(datetime(2026, 9, 17, 12, 0))


def test_require_aware_returns_value():
    value = datetime(2026, 9, 17, tzinfo=UTC)
    assert require_aware(value, "value") is value
