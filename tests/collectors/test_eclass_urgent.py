"""급한 소식 판정 확인.

마감은 날짜로 알지만 휴강·시험 변경은 글을 읽어야 안다.
**애매하면 즉시 알리지 않는다.** 놓치는 것보다 잘못 울리는 쪽이 더 성가시다.
"""

from pathlib import Path

import pytest

from app.collectors.eclass.urgent import BODY_WINDOW, is_urgent
from app.core.config import DEFAULT_URGENT_WORDS, load_settings


@pytest.mark.parametrize(
    "title",
    [
        "[매우 중요] 9월 23일 수업 휴강 --> 온라인보강",
        "10월 2일 결강 안내",
        "중간고사 일정 변경 안내",
        "강의실 변경 공지",
        "기말고사 연기",
        "[긴급] 오늘 수업 취소",
    ],
)
def test_a_class_that_moves_or_disappears_is_urgent(title):
    assert is_urgent(title) is True


@pytest.mark.parametrize(
    "title",
    [
        "1차 퀴즈 결과 안내",
        "9월 16일 수업자료",
        "교재 구매 안내",
        "팀 편성 결과",
    ],
)
def test_an_ordinary_notice_waits_for_the_briefing(title):
    assert is_urgent(title) is False


def test_the_body_is_read_too():
    assert is_urgent("수업 안내", "다음 주 수요일은 휴강입니다.") is True


def test_only_the_start_of_a_long_body_is_read():
    """본문 끝에 스친 낱말로 울리지 않는다."""
    buried = "가" * (BODY_WINDOW + 50) + " 휴강"
    assert is_urgent("수업 안내", buried) is False


def test_an_empty_word_list_never_calls_anything_urgent():
    assert is_urgent("휴강 안내", words=()) is False


CONFIG = """
[telegram]
bot_token = "t"
allowed_user_id = "1"

[storage]
db_path = "x.db"
"""


def write_config(tmp_path, extra: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG + extra, encoding="utf-8")
    return path


def test_the_words_can_be_changed_in_the_settings(tmp_path):
    """개인정보가 아니라서 config.toml에 둔다. 기기에 있는 설정을 읽지 않는다."""
    config = write_config(tmp_path, '\n[eclass]\nurgent_words = ["특강"]\n')
    settings = load_settings(config, env={}, local_path=tmp_path / "없음.toml")

    assert settings.eclass.urgent_words == ("특강",)
    assert is_urgent("특강 안내", words=settings.eclass.urgent_words) is True


def test_the_default_words_are_used_when_nothing_is_set(tmp_path):
    settings = load_settings(write_config(tmp_path), env={}, local_path=tmp_path / "없음.toml")
    assert settings.eclass.urgent_words == DEFAULT_URGENT_WORDS
