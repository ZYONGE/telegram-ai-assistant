from datetime import time
from pathlib import Path

import pytest

from app.core.config import ConfigError, load_settings, resolve_env_refs

CONFIG = """
[telegram]
bot_token = "${TOKEN}"
allowed_user_id = "${USER_ID}"

[notification]
quiet_start = "23:30"
daily_limit = 3

[storage]
db_path = "data/assistant.db"
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
    assert settings.storage.db_path == tmp_path / "data" / "assistant.db"


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
    settings = load_settings(project_config, env={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_USER_ID": "7"})
    assert settings.notification.quiet_end == time(6, 30)
