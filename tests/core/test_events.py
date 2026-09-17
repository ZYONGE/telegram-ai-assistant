from datetime import UTC, datetime

import pytest

from app.core.events import (
    META_USER_REQUESTED,
    Event,
    EventKind,
    EventSource,
    collector_failed,
)


def make_event(**overrides):
    fields = dict(source=EventSource.ECLASS, kind=EventKind.DEADLINE, title="과제 마감", ref_id="eclass:report:K1:1")
    return Event(**(fields | overrides))


def test_event_defaults():
    event = make_event()
    assert event.urgent is False
    assert event.due_at is None
    assert event.meta == {}
    assert event.user_requested is False


@pytest.mark.parametrize("name", ["source", "kind", "title", "ref_id"])
def test_event_rejects_empty_required_field(name):
    with pytest.raises(ValueError):
        make_event(**{name: ""})


def test_event_rejects_naive_due_at():
    with pytest.raises(ValueError):
        make_event(due_at=datetime(2026, 9, 18, 23, 59))


def test_event_accepts_aware_due_at():
    due = datetime(2026, 9, 18, 14, 59, tzinfo=UTC)
    assert make_event(due_at=due).due_at == due


def test_user_requested_flag_comes_from_meta():
    assert make_event(meta={META_USER_REQUESTED: True}).user_requested is True


def test_str_enums_compare_equal_to_plain_strings():
    event = make_event(source="eclass", kind="deadline")
    assert event.source == EventSource.ECLASS
    assert event.kind == EventKind.DEADLINE


def test_collector_failed_uses_stable_ref_id_per_reason():
    first = collector_failed("eclass", "login_failed", "비밀번호 오류")
    second = collector_failed("eclass", "login_failed")
    other = collector_failed("eclass", "captcha")

    assert first.kind == EventKind.COLLECTOR_FAILED
    assert first.ref_id == second.ref_id
    assert first.ref_id != other.ref_id
    assert first.meta["reason"] == "login_failed"
