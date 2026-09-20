"""eClass 수집 범위 결정 확인.

핵심은 하나다: **알림이 넘치지 않아야 한다.** FAQ 74줄·설문 20줄까지 알리면
하루 상한을 오전에 다 쓴다. 규칙만으로도 끝까지 돌아가야 하고, 모델은 거들기만 한다.
"""

import json
from dataclasses import replace

import pytest

from app.collectors.eclass.scope import (
    Decided,
    Screen,
    ScopeEntry,
    ScopeStore,
    classify,
    collected,
    decide,
    ensure_scope,
    load_catalog,
)
from app.core.config import Level, ScopePolicy

POLICY = ScopePolicy(
    unknown=Level.OFF,
    notify_paths=("notice", "report_list", "test_list", "todo_list", "message", "calendar"),
    store_paths=("material", "plan_form", "attendance", "eval", "qna"),
    off_paths=("guide", "survey", "share", "ocw", "myinfo"),
    notify_words=("공지", "과제", "시험", "쪽지"),
    store_words=("자료", "게시판", "계획서", "성적"),
    off_words=("FAQ", "설문", "소모임"),
)


def screen(path: str, name: str = "", **overrides) -> Screen:
    return Screen(path=path, name=name, **{"listing": True, **overrides})


# --- 코드 규칙 ---


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # 놓치면 곤란한 것
        ("/ilos/st/course/notice_list.acl", Level.NOTIFY),
        ("/ilos/st/course/report_list.acl", Level.NOTIFY),
        ("/ilos/st/course/test_list_form.acl", Level.NOTIFY),
        ("/ilos/message/received_list_pop_form.acl", Level.NOTIFY),
        ("/ilos/st/schedule/academic_calendar_list_form.acl", Level.NOTIFY),
        # 물어보면 답하면 되는 것
        ("/ilos/st/course/lecture_material_list.acl", Level.STORE),
        ("/ilos/st/course/plan_form.acl", Level.STORE),
        ("/ilos/community/qna_list.acl", Level.STORE),
        # 비서가 건드릴 일 없는 것
        ("/ilos/guide/guide_main_form.acl", Level.OFF),
        ("/ilos/community/total_survey_list_form.acl", Level.OFF),
        ("/ilos/community/share_group_list.acl", Level.OFF),
        ("/ilos/mp/myinfo_form.acl", Level.OFF),
    ],
)
def test_the_path_decides_the_obvious_screens(path, expected):
    assert classify(screen(path), POLICY) is expected


def test_the_menu_name_is_used_when_the_path_says_nothing():
    assert classify(screen("/ilos/etc/board01.acl", "학과 공지"), POLICY) is Level.NOTIFY
    assert classify(screen("/ilos/etc/board02.acl", "강의 자료"), POLICY) is Level.STORE
    assert classify(screen("/ilos/etc/board03.acl", "만족도 설문"), POLICY) is Level.OFF


def test_a_deadline_is_worth_telling_about():
    assert classify(screen("/ilos/etc/board04.acl", has_due=True), POLICY) is Level.NOTIFY


def test_a_dated_list_goes_to_the_briefing():
    assert classify(screen("/ilos/etc/board05.acl", has_date=True), POLICY) is Level.BRIEF


def test_a_screen_with_nothing_to_read_is_left_alone():
    assert classify(screen("/ilos/etc/board06.acl", listing=False), POLICY) is Level.OFF


def test_a_single_article_is_not_a_source():
    """글 하나를 여는 화면은 목록이 아니다. 제목이 그럴듯해도 수집하지 않는다."""
    single = screen("/ilos/community/notice_view_form.acl", "휴강 안내", has_date=True)
    assert classify(single, POLICY) is Level.OFF


def test_a_deadline_does_not_override_a_screen_we_turned_off():
    """설문에도 마감이 있지만 알릴 일은 아니다."""
    survey = screen("/ilos/community/total_survey_list_form.acl", "설문", has_due=True)
    assert classify(survey, POLICY) is Level.OFF


def test_an_unfamiliar_screen_is_left_to_someone_else():
    assert classify(screen("/ilos/etc/board07.acl"), POLICY) is None


# --- 전체 결정 ---


class FakeClassifier:
    def __init__(self, answers: dict[str, Level] | None = None, fails: bool = False) -> None:
        self.answers = answers or {}
        self.fails = fails
        self.asked: list[str] = []

    async def classify_screens(self, screens: list[Screen]) -> dict[str, Level]:
        self.asked = [item.path for item in screens]
        if self.fails:
            raise RuntimeError("모델이 응답하지 않습니다")
        return self.answers


CATALOG = [
    screen("/ilos/st/course/notice_list.acl", "공지사항"),
    screen("/ilos/guide/guide_main_form.acl", "FAQ", has_due=True),
    screen("/ilos/etc/board07.acl", "무엇인가"),
]


async def test_rules_alone_get_through_the_whole_catalog():
    """모델이 없어도 끝까지 정해진다. 수집이 모델에 매이지 않는다."""
    scope = await decide(CATALOG, POLICY)

    assert scope["/ilos/st/course/notice_list.acl"].level is Level.NOTIFY
    assert scope["/ilos/guide/guide_main_form.acl"].level is Level.OFF
    # 규칙이 못 정한 것은 설정의 기본값으로
    assert scope["/ilos/etc/board07.acl"].level is Level.OFF
    assert all(entry.by is Decided.RULE for entry in scope.values())


async def test_only_the_leftovers_go_to_the_model():
    classifier = FakeClassifier({"/ilos/etc/board07.acl": Level.STORE})
    scope = await decide(CATALOG, POLICY, classifier=classifier)

    assert classifier.asked == ["/ilos/etc/board07.acl"]
    assert scope["/ilos/etc/board07.acl"].level is Level.STORE
    assert scope["/ilos/etc/board07.acl"].by is Decided.MODEL


async def test_a_broken_model_does_not_stop_the_decision():
    scope = await decide(CATALOG, POLICY, classifier=FakeClassifier(fails=True))
    assert scope["/ilos/etc/board07.acl"].level is Level.OFF
    assert len(scope) == 3


async def test_an_answer_we_do_not_understand_falls_back():
    classifier = FakeClassifier({"/ilos/etc/board99.acl": Level.NOTIFY})
    scope = await decide(CATALOG, POLICY, classifier=classifier)
    assert scope["/ilos/etc/board07.acl"].level is Level.OFF


async def test_what_the_user_decided_is_never_overwritten():
    kept = {
        "/ilos/st/course/notice_list.acl": ScopeEntry(
            path="/ilos/st/course/notice_list.acl", name="공지사항", level=Level.BRIEF, by=Decided.USER
        )
    }
    scope = await decide(CATALOG, POLICY, existing=kept)
    assert scope["/ilos/st/course/notice_list.acl"].level is Level.BRIEF


async def test_a_users_choice_survives_a_catalog_without_that_screen():
    """과목방에 못 들어간 날에도 사용자가 정해 둔 것은 남는다."""
    kept = {
        "/ilos/st/course/report_list.acl": ScopeEntry(
            path="/ilos/st/course/report_list.acl", name="과제", level=Level.OFF, by=Decided.USER
        )
    }
    scope = await decide(CATALOG, POLICY, existing=kept)
    assert scope["/ilos/st/course/report_list.acl"].level is Level.OFF


async def test_what_we_decided_before_is_decided_again():
    """사용자가 정한 것이 아니면 규칙이 바뀌었을 때 따라간다."""
    stale = {
        "/ilos/st/course/notice_list.acl": ScopeEntry(
            path="/ilos/st/course/notice_list.acl", name="공지사항", level=Level.OFF, by=Decided.MODEL
        )
    }
    scope = await decide(CATALOG, POLICY, existing=stale)
    assert scope["/ilos/st/course/notice_list.acl"].level is Level.NOTIFY


async def test_collected_screens_come_in_order_of_urgency():
    scope = await decide(
        [
            screen("/ilos/st/course/lecture_material_list.acl", "강의자료"),
            screen("/ilos/st/course/notice_list.acl", "공지사항"),
            screen("/ilos/guide/guide_main_form.acl", "FAQ"),
        ],
        POLICY,
    )
    order = [entry.level for entry in collected(scope)]
    assert order == [Level.NOTIFY, Level.STORE]


# --- 카탈로그와 저장 ---


def test_the_catalog_is_read_without_the_screens_that_would_not_open(tmp_path):
    path = tmp_path / "eclass_catalog.json"
    path.write_text(
        json.dumps(
            {
                "screens": [
                    {"path": "/ilos/a_list.acl", "name": "가", "listing": True, "has_due": True},
                    {"path": "/ilos/b_list.acl", "name": "나", "note": "열리지 않음(network)"},
                    {"path": "", "name": "이름뿐"},
                ]
            }
        ),
        encoding="utf-8",
    )
    screens = load_catalog(path)
    assert [item.path for item in screens] == ["/ilos/a_list.acl"]
    assert screens[0].has_due is True


def test_a_missing_catalog_is_not_an_error(tmp_path):
    assert load_catalog(tmp_path / "없는파일.json") == []


async def test_the_decision_survives_a_restart(tmp_path):
    store = ScopeStore(tmp_path / "eclass_scope.json")
    assert store.read() == {}

    store.write(await decide(CATALOG, POLICY))
    again = store.read()
    assert again["/ilos/st/course/notice_list.acl"].level is Level.NOTIFY
    assert again["/ilos/st/course/notice_list.acl"].name == "공지사항"


def test_changing_a_level_marks_it_as_the_users_own(tmp_path):
    store = ScopeStore(tmp_path / "eclass_scope.json")
    store.write(
        {
            "/ilos/st/course/notice_list.acl": ScopeEntry(
                path="/ilos/st/course/notice_list.acl", name="공지사항", level=Level.NOTIFY
            )
        }
    )
    saved = store.set_level("/ilos/st/course/notice_list.acl", Level.BRIEF)

    assert saved.by is Decided.USER and saved.name == "공지사항"
    assert store.read()["/ilos/st/course/notice_list.acl"].level is Level.BRIEF


def test_a_damaged_scope_file_is_decided_again(tmp_path):
    path = tmp_path / "eclass_scope.json"
    path.write_text("{망가진 파일", encoding="utf-8")
    assert ScopeStore(path).read() == {}


def test_a_screen_that_is_not_a_list_is_off_even_with_a_deadline():
    """메인 화면에도 '마감'이라는 글자가 있다. 목록이 아니면 수집원이 아니다."""
    main = screen("/ilos/main/main_form.acl", listing=False, has_due=True)
    assert classify(main, POLICY) is Level.OFF


def test_turning_something_off_wins_over_a_topic_word():
    """보낸 쪽지는 '쪽지'이지만 내가 보낸 것이라 알릴 일이 없다."""
    policy = replace(POLICY, off_paths=POLICY.off_paths + ("sent_list",))
    assert classify(screen("/ilos/message/sent_list_pop_form.acl", "보낸쪽지"), policy) is Level.OFF


# --- 시작할 때 ---


def write_catalog(path, screens):
    path.write_text(
        json.dumps({"screens": [{"path": s.path, "name": s.name, "listing": s.listing} for s in screens]}),
        encoding="utf-8",
    )


async def test_the_scope_is_decided_the_first_time(tmp_path):
    catalog = tmp_path / "eclass_catalog.json"
    write_catalog(catalog, CATALOG)
    store = ScopeStore(tmp_path / "eclass_scope.json")

    scope = await ensure_scope(catalog, POLICY, store)
    assert scope["/ilos/st/course/notice_list.acl"].level is Level.NOTIFY
    assert store.read() == scope


async def test_nothing_is_decided_again_when_no_screen_is_new(tmp_path):
    catalog = tmp_path / "eclass_catalog.json"
    write_catalog(catalog, CATALOG)
    store = ScopeStore(tmp_path / "eclass_scope.json")
    await ensure_scope(catalog, POLICY, store)

    classifier = FakeClassifier()
    await ensure_scope(catalog, POLICY, store, classifier)
    assert classifier.asked == []


async def test_a_new_screen_starts_a_new_decision(tmp_path):
    catalog = tmp_path / "eclass_catalog.json"
    write_catalog(catalog, CATALOG)
    store = ScopeStore(tmp_path / "eclass_scope.json")
    await ensure_scope(catalog, POLICY, store)

    write_catalog(catalog, [*CATALOG, screen("/ilos/st/course/zoom_list.acl", "실시간강의")])
    scope = await ensure_scope(catalog, POLICY, store)
    assert "/ilos/st/course/zoom_list.acl" in scope


async def test_without_a_catalog_nothing_happens(tmp_path):
    store = ScopeStore(tmp_path / "eclass_scope.json")
    assert await ensure_scope(tmp_path / "없음.json", POLICY, store) == {}
    assert not (tmp_path / "eclass_scope.json").exists()
