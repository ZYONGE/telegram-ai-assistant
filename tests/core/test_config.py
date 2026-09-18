from datetime import time
from pathlib import Path

import pytest

from app.core.config import ConfigError, load_settings, merge_settings, resolve_env_refs

CONFIG = """
[telegram]
bot_token = "${TOKEN}"
allowed_user_id = "${USER_ID}"

[notification]
quiet_start = "23:30"
daily_limit = 3

[storage]
db_path = "private/assistant.db"
"""


def write_config(tmp_path: Path, text: str = CONFIG) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_settings_resolves_env_and_defaults(tmp_path):
    settings = load_settings(write_config(tmp_path), env={"TOKEN": "abc:123", "USER_ID": "42"})

    assert settings.telegram.bot_token == "abc:123"
    assert settings.telegram.allowed_user_id == 42
    assert settings.notification.quiet_start == time(23, 30)
    assert settings.notification.quiet_end == time(6, 30)
    assert settings.notification.daily_limit == 3
    assert settings.storage.db_path == tmp_path / "private" / "assistant.db"
    assert settings.storage.profile_path == tmp_path / "private" / "profile.md"
    assert settings.storage.memory_path == tmp_path / "private" / "memory.md"


def test_missing_env_vars_are_reported_together_without_values(tmp_path):
    with pytest.raises(ConfigError) as error:
        load_settings(write_config(tmp_path), env={"UNRELATED": "secret-value"})
    message = str(error.value)
    assert "telegram.bot_token → TOKEN" in message
    assert "telegram.allowed_user_id → USER_ID" in message
    assert "secret-value" not in message


def test_invalid_user_id_is_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(write_config(tmp_path), env={"TOKEN": "t", "USER_ID": "not-a-number"})


def test_missing_config_file_is_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "nope.toml", env={})


def test_resolve_env_refs_walks_nested_values():
    data = {"a": ["x-${A}", {"b": "${B}"}], "n": 1}
    assert resolve_env_refs(data, {"A": "1", "B": "2"}) == {"a": ["x-1", {"b": "2"}], "n": 1}


def test_project_config_file_loads_with_env(tmp_path):
    project_config = Path(__file__).parents[2] / "config.toml"
    settings = load_settings(
        project_config,
        env={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_USER_ID": "7"},
        local_path=tmp_path / "없음.toml",
    )
    # 개인 데이터는 git에서 제외된 private/ 폴더 하나에 모인다
    assert settings.storage.db_path.parent.name == "private"
    assert settings.storage.profile_path.parent.name == "private"
    assert settings.notification.quiet_end == time(6, 30)
    assert settings.llm.provider == "gemini"
    assert settings.llm.chat_model == settings.llm.light_model == "gemini-3.5-flash-lite"
    assert settings.llm.options["backend"] == "api_key"
    # 현재 운영 상태: 결제 미연결, 무료 티어 명시 허용
    assert settings.llm.options["billing_enabled"] is False
    assert settings.llm.options["allow_free_tier"] is True


def test_llm_section_defaults_and_provider_options(tmp_path):
    minimal = load_settings(write_config(tmp_path), env={"TOKEN": "t", "USER_ID": "1"})
    assert minimal.llm.provider == "gemini" and minimal.llm.options == {}

    text = CONFIG + """
[llm]
provider = "other"
chat_model = "big"

[llm.other]
region = "kr"

[llm.gemini]
backend = "vertex"
"""
    custom = load_settings(write_config(tmp_path, text), env={"TOKEN": "t", "USER_ID": "1"})
    assert custom.llm.provider == "other"
    assert custom.llm.chat_model == "big" and custom.llm.light_model == "gemini-3.5-flash-lite"
    assert custom.llm.options == {"region": "kr"}


def test_local_file_overrides_only_given_keys(tmp_path):
    local = tmp_path / "local.toml"
    local.write_text(
        """
[notification]
daily_limit = 9

[briefing]
morning = "08:10"
""",
        encoding="utf-8",
    )

    settings = load_settings(write_config(tmp_path), env={"TOKEN": "t", "USER_ID": "1"}, local_path=local)
    assert settings.notification.daily_limit == 9
    assert settings.notification.quiet_start == time(23, 30)
    assert settings.briefing.morning == time(8, 10)
    assert settings.briefing.evening == time(22, 0)


def test_missing_local_file_is_ignored(tmp_path):
    settings = load_settings(
        write_config(tmp_path), env={"TOKEN": "t", "USER_ID": "1"}, local_path=tmp_path / "없음.toml"
    )
    assert settings.notification.daily_limit == 3


def test_broken_local_file_is_config_error(tmp_path):
    local = tmp_path / "local.toml"
    local.write_text("daily_limit = ", encoding="utf-8")
    with pytest.raises(ConfigError, match="개인 설정"):
        load_settings(write_config(tmp_path), env={"TOKEN": "t", "USER_ID": "1"}, local_path=local)


def test_merge_settings_keeps_nested_values():
    base = {"llm": {"provider": "gemini", "gemini": {"backend": "api_key", "project": ""}}, "a": 1}
    merged = merge_settings(base, {"llm": {"gemini": {"project": "p"}}})
    assert merged == {"llm": {"provider": "gemini", "gemini": {"backend": "api_key", "project": "p"}}, "a": 1}
    assert base["llm"]["gemini"]["project"] == ""
