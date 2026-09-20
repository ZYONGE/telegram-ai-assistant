from datetime import timedelta

import pytest

from app.storage.eclass import (
    MAX_LOGIN_FAILURES,
    EclassHealthStore,
    EclassItem,
    EclassRepository,
    EclassSourceStateStore,
    ItemChange,
)
from tests.conftest import kst

NOW = kst(9, 20, 9)


@pytest.fixture
def items(db):
    return EclassRepository(db)


@pytest.fixture
def health(db):
    return EclassHealthStore(db)


def assignment(due=None, title="과제 2 제출", item_id="eclass:assignment:cs101:7") -> EclassItem:
    return EclassItem(
        item_id=item_id,
        kind="assignment",
        title=title,
        course="자료구조",
        due_at=due,
        url="https://example.ac.kr/assignment/7",
    )


# --- 본 글과 마감 변경 ---


async def test_first_sight_is_new_and_second_is_same(items):
    assert await items.upsert(assignment(kst(9, 25, 23, 59)), NOW) is ItemChange.NEW
    assert await items.upsert(assignment(kst(9, 25, 23, 59)), NOW) is ItemChange.SAME


async def test_changed_due_date_is_reported_once(items):
    await items.upsert(assignment(kst(9, 25, 23, 59)), NOW)

    assert await items.upsert(assignment(kst(9, 27, 23, 59)), NOW) is ItemChange.DUE_CHANGED
    # 바뀐 값으로 저장되었으니 다음 수집에서는 조용하다
    assert await items.upsert(assignment(kst(9, 27, 23, 59)), NOW) is ItemChange.SAME
    assert (await items.get("eclass:assignment:cs101:7")).due_at == kst(9, 27, 23, 59)


async def test_due_removed_or_added_counts_as_a_change(items):
    await items.upsert(assignment(kst(9, 25, 23, 59)), NOW)
    assert await items.upsert(assignment(None), NOW) is ItemChange.DUE_CHANGED
    assert await items.upsert(assignment(kst(9, 25, 23, 59)), NOW) is ItemChange.DUE_CHANGED


async def test_title_change_alone_is_not_an_alert(items):
    await items.upsert(assignment(kst(9, 25, 23, 59)), NOW)
    assert await items.upsert(assignment(kst(9, 25, 23, 59), title="과제 2 제출 (수정)"), NOW) is ItemChange.SAME
    assert (await items.get("eclass:assignment:cs101:7")).title.endswith("(수정)")


async def test_first_seen_time_is_kept(items):
    await items.upsert(assignment(kst(9, 25, 23, 59)), NOW)
    await items.upsert(assignment(kst(9, 26, 23, 59)), kst(9, 21, 9))
    stored = await items.get("eclass:assignment:cs101:7")
    assert stored.first_seen_at == NOW and stored.updated_at == kst(9, 21, 9)


async def test_due_between_lists_in_order(items):
    await items.upsert(assignment(kst(9, 25, 23, 59), item_id="a"), NOW)
    await items.upsert(assignment(kst(9, 22, 12), item_id="b"), NOW)
    await items.upsert(assignment(None, item_id="c"), NOW)
    await items.upsert(assignment(kst(10, 2, 12), item_id="d"), NOW)

    found = await items.due_between(NOW, kst(9, 27, 0))
    assert [item.item_id for item in found] == ["b", "a"]
    assert await items.count() == 4


# --- 수집 건강 상태 ---


async def test_failures_of_the_same_reason_add_up(health):
    assert await health.record_failure("login", NOW) == 1
    assert await health.record_failure("login", NOW) == 2
    state = await health.read()
    assert state.fail_count == 2 and state.last_reason == "login"
    # 로그인 연속 2회 실패면 자동화를 멈춘다
    assert state.login_blocked is True and MAX_LOGIN_FAILURES == 2


async def test_a_different_reason_starts_over(health):
    await health.record_failure("network", NOW)
    await health.record_failure("network", NOW)
    assert await health.record_failure("layout", NOW) == 1
    assert (await health.read()).login_blocked is False


async def test_success_clears_the_counter(health):
    await health.record_failure("login", NOW)
    await health.record_success(NOW)
    state = await health.read()
    assert state.fail_count == 0 and state.last_reason == "" and state.last_ok_at == NOW
    assert state.login_blocked is False


async def test_failure_keeps_the_last_success_time(health):
    await health.record_success(NOW)
    await health.record_failure("network", kst(9, 20, 10))
    assert (await health.read()).last_ok_at == NOW


async def test_stale_only_after_a_first_success(health):
    assert (await health.read()).stale(kst(9, 25, 9), hours=12) is False

    await health.record_success(NOW)
    state = await health.read()
    assert state.stale(kst(9, 20, 20), hours=12) is False
    assert state.stale(kst(9, 20, 22), hours=12) is True


async def test_clear_lets_the_user_retry_after_fixing_the_password(health):
    await health.record_failure("login", NOW)
    await health.record_failure("login", NOW)
    await health.clear()
    assert (await health.read()).login_blocked is False


# --- 소스별 주기 ---


@pytest.fixture
def source_state(db):
    return EclassSourceStateStore(db)


async def test_a_source_runs_the_first_time(source_state):
    assert await source_state.due("todo", timedelta(hours=24), NOW) is True


async def test_a_source_waits_out_its_interval(source_state):
    await source_state.record_run("todo", NOW, ok=True)
    assert await source_state.due("todo", timedelta(hours=24), NOW + timedelta(hours=6)) is False
    assert await source_state.due("todo", timedelta(hours=24), NOW + timedelta(hours=24)) is True


async def test_a_few_seconds_late_does_not_skip_a_whole_turn(source_state):
    """예약이 몇 초 밀려 들어와도 한 주기를 통째로 건너뛰지 않는다."""
    await source_state.record_run("todo", NOW, ok=True)
    almost = NOW + timedelta(hours=24) - timedelta(seconds=20)
    assert await source_state.due("todo", timedelta(hours=24), almost) is True


async def test_no_interval_means_every_turn(source_state):
    await source_state.record_run("todo", NOW, ok=True)
    assert await source_state.due("todo", timedelta(0), NOW) is True


async def test_a_failed_run_keeps_the_last_success(source_state):
    await source_state.record_run("todo", NOW, ok=True)
    await source_state.record_run("todo", NOW + timedelta(hours=1), ok=False, reason="layout")

    state = await source_state.read("todo")
    assert state.last_ok_at == NOW and state.last_run_at == NOW + timedelta(hours=1)
    assert state.last_reason == "layout"


async def test_sources_are_counted_separately(source_state):
    await source_state.record_run("todo", NOW, ok=True)
    assert await source_state.due("notice", timedelta(hours=24), NOW) is True
