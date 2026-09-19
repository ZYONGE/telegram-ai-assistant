"""커밋 전 개인정보 검사기 (scripts/check_private.py) 확인.

이 파일에는 형태 검사를 확인하려고 만든 가짜 키가 들어 있다: check-private: 예시 값
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from scripts.check_private import (  # noqa: E402
    FAKE_MARKER,
    KEY_PATTERNS,
    blocked_path,
    check,
    find_leaks,
    secret_values,
)

VALUES = {"프로필의 이름": "홍길동", "프로필의 학교": "예시대학교", ".env의 GEMINI_API_KEY": "secret-key-value"}


@pytest.fixture
def private(tmp_path):
    folder = tmp_path / "private"
    folder.mkdir()
    (folder / ".env").write_text(
        "# 주석\nTELEGRAM_BOT_TOKEN=123456:ABCDEF-token-value\nEMPTY=\n", encoding="utf-8"
    )
    (folder / "profile.md").write_text(
        "# 사용자 프로필\n- 이름: 홍길동\n- 호칭: 길동님\n- 학교: 예시대학교\n- 학과(전공):\n", encoding="utf-8"
    )
    (folder / "local.toml").write_text('[weather]\nplace = "우리 동네 이름"\n', encoding="utf-8")
    return tmp_path


def test_values_are_collected_from_every_private_file(private):
    values = secret_values(private)
    assert values[".env의 TELEGRAM_BOT_TOKEN"] == "123456:ABCDEF-token-value"
    assert values["프로필의 이름"] == "홍길동" and values["프로필의 호칭"] == "길동님"
    assert values["프로필의 학교"] == "예시대학교"
    assert values["local.toml의 place"] == "우리 동네 이름"
    # 빈 값과 빈 항목은 대조 대상이 아니다
    assert ".env의 EMPTY" not in values
    assert "프로필의 학과(전공)" not in values


def test_missing_private_files_are_fine(tmp_path):
    assert secret_values(tmp_path) == {}


def test_leaks_report_the_label_not_the_value():
    found = find_leaks("안녕하세요 홍길동님, 예시대학교 학생이시죠", VALUES)
    assert found == ["프로필의 이름", "프로필의 학교"]
    assert all("홍길동" not in item and "예시대학교" not in item for item in found)


def test_clean_text_passes():
    assert find_leaks("사용자님, {honorific} 자리표시만 씁니다", VALUES) == []


@pytest.mark.parametrize(
    "text",
    [
        "key = AIzaSyA1234567890abcdefghijklmnopqrstu",
        "token = 123456789:AAFFzz-0123456789abcdefghijklmnopqrs",
        "Authorization: Bearer ya29.a0AfH6SMBx1234567890abcdef",
        "client_secret = GOCSPX-abcdef123456",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_key_shaped_strings_are_caught_even_without_private_files(text):
    found = find_leaks(text, {})
    assert len(found) == 1 and found[0].endswith("형태의 문자열")


def test_key_patterns_do_not_fire_on_ordinary_text():
    ordinary = "2026-09-20 18:00 면담, 전화 010-0000-0000, 버전 3.14"
    assert not any(pattern.search(ordinary) for pattern in KEY_PATTERNS.values())


@pytest.mark.parametrize(
    ("path", "blocked"),
    [
        ("private/profile.md", True),
        ("private/google_token_1.json", True),
        (".env", True),
        (".env.local", True),
        ("templates/env.example", False),
        ("app/main.py", False),
        ("docs/privacy.html", False),
    ],
)
def test_private_paths_are_blocked_outright(path, blocked):
    assert (blocked_path(path) is not None) is blocked


def test_check_reports_each_problem_file(monkeypatch):
    import scripts.check_private as guard

    monkeypatch.setattr(guard, "staged_text", lambda path: {"a.py": "홍길동", "b.py": "사용자"}[path])
    problems = check(["a.py", "b.py", "private/.env"], VALUES)
    assert len(problems) == 2
    assert problems[0].startswith("a.py:") and "이름" in problems[0]
    assert problems[1].startswith("private/.env:")
    assert "홍길동" not in " ".join(problems)


def test_marked_files_skip_the_shape_check_but_not_real_values():
    text = f"# {FAKE_MARKER}\nkey = AIzaSyA1234567890abcdefghijklmnopqrstu"
    assert find_leaks(text, VALUES) == []
    # 표시가 있어도 실제 private/ 값이 들어 있으면 여전히 막는다
    assert find_leaks(f"{text}\n홍길동", VALUES) == ["프로필의 이름"]
