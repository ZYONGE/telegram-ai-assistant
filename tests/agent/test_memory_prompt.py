import pytest

from app.agent.memory import MarkdownMemoryStore, sensitive_reason
from app.agent.prompt import PromptBuilder, clean_profile, format_now, load_identity, parse_identity
from tests.conftest import kst


@pytest.fixture
def memory(tmp_path):
    return MarkdownMemoryStore(tmp_path / "data" / "memory.md")


async def test_memory_add_read_delete_persists_to_markdown(memory, tmp_path):
    item = await memory.add("  월요일 오전에는\n수업이 없음 ", kst(9, 17, 14))
    assert item.text == "월요일 오전에는 수업이 없음"

    reopened = MarkdownMemoryStore(tmp_path / "data" / "memory.md")
    assert [(i.item_id, i.text) for i in await reopened.read()] == [(item.item_id, item.text)]
    content = (tmp_path / "data" / "memory.md").read_text(encoding="utf-8")
    assert f"- [{item.item_id}] (2026-09-17) 월요일 오전에는 수업이 없음" in content

    assert await memory.delete(item.item_id) is True
    assert await memory.delete(item.item_id) is False
    assert await memory.read() == []


@pytest.mark.parametrize(
    "text",
    [
        "주민번호 010101-3123456",
        "카드 1234 5678 9012 3456",
        "계좌 110-123-456789",
        "포털 비밀번호는 abcd",
        "여권 번호 M12345678",
    ],
)
async def test_sensitive_information_is_not_recorded(memory, text):
    assert sensitive_reason(text) is not None
    with pytest.raises(ValueError):
        await memory.add(text, kst(9, 17, 14))
    assert await memory.read() == []


@pytest.mark.parametrize(
    "text", ["9월 20일 18시에 면접", "2026-09-20 18:00에 면접", "토익 점수 900점", "010으로 시작하는 번호는 저장 안 함"]
)
def test_ordinary_text_is_not_sensitive(text):
    assert sensitive_reason(text) is None


async def test_empty_memory_is_rejected(memory):
    with pytest.raises(ValueError):
        await memory.add("   ", kst(9, 17, 14))


def test_clean_profile_drops_comments_and_empty_fields():
    profile = """# 사용자 프로필

<!-- 작성 안내: 비밀번호 쓰지 말 것 -->

## 1. 기본 정보

- 이름: 홍길동
- 학과(전공):
- 학년: 3학년

| 과목 | 요일 |
|---|---|
|  |  |

## 10. 기타 메모

-
"""
    cleaned = clean_profile(profile)
    assert "작성 안내" not in cleaned
    assert "학과" not in cleaned
    assert "- 이름: 홍길동" in cleaned and "- 학년: 3학년" in cleaned
    assert "|  |  |" not in cleaned
    assert "\n\n\n" not in cleaned


def test_format_now_uses_kst_with_weekday():
    assert format_now(kst(9, 18, 7, 5)) == "2026-09-18(금) 07:05 (Asia/Seoul)"


async def test_prompt_builder_fills_placeholders(tmp_path, memory):
    template = tmp_path / "system.md"
    template.write_text(
        "<!-- 주석 -->\n호칭: {honorific}\n프로필:\n{profile}\n기억:\n{memory}\n요약:\n{summary}", encoding="utf-8"
    )
    profile = tmp_path / "profile.md"
    profile.write_text("- 이름: 홍길동\n- 호칭: 길동님\n- 학과:\n", encoding="utf-8")
    item = await memory.add("아침형 인간", kst(9, 17, 14))

    prompt = await PromptBuilder(template, profile, memory).build("- 보고서 이야기 중")

    assert "주석" not in prompt
    assert "호칭: 길동님" in prompt
    assert "- 이름: 홍길동" in prompt and "학과" not in prompt
    assert f"- [{item.item_id}] 아침형 인간" in prompt
    assert "- 보고서 이야기 중" in prompt


async def test_prompt_builder_handles_missing_profile_and_empty_state(tmp_path, memory):
    template = tmp_path / "system.md"
    template.write_text("{honorific}|{profile}|{memory}|{summary}", encoding="utf-8")
    prompt = await PromptBuilder(template, tmp_path / "없음.md", memory).build("")
    assert prompt == "사용자님|(아직 작성되지 않음)|(아직 없음)|(없음)"


@pytest.mark.parametrize(
    ("profile", "name", "honorific"),
    [
        ("- 이름: 홍길동\n- 호칭: 길동님\n", "홍길동", "길동님"),
        ("- 이름: 홍길동\n- 호칭:\n", "홍길동", "홍길동님"),
        ("<!-- - 호칭: 예시님 -->\n- 학년: 3학년\n", "", "사용자님"),
    ],
)
def test_parse_identity(profile, name, honorific):
    identity = parse_identity(profile)
    assert (identity.name, identity.honorific) == (name, honorific)


def test_load_identity_without_profile(tmp_path):
    assert load_identity(tmp_path / "없음.md").honorific == "사용자님"


def test_project_system_prompt_has_all_placeholders():
    from pathlib import Path

    text = (Path(__file__).parents[2] / "prompts" / "system_prompt.md").read_text(encoding="utf-8")
    for placeholder in ("{honorific}", "{profile}", "{memory}", "{summary}"):
        assert placeholder in text
    assert "{current_datetime}" not in text
