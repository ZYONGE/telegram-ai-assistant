from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.error import InvalidToken

from app import main as app_main
from app.core.config import (
    BriefingSettings,
    ConfigError,
    ConversationSettings,
    LLMSettings,
    NotificationSettings,
    Settings,
    StorageSettings,
    TelegramSettings,
    WeatherSettings,
)

SECRET = "123456:SECRET-TOKEN-VALUE"


class RejectingApplication:
    def run_polling(self, **kwargs):
        raise InvalidToken(f"The token `{SECRET}` was rejected by the server.")


def test_rejected_token_is_not_printed(monkeypatch, capsys, caplog):
    monkeypatch.setattr(app_main, "load_settings", lambda: object())
    monkeypatch.setattr(app_main, "build_application", lambda settings: RejectingApplication())

    with pytest.raises(SystemExit) as exit_info:
        app_main.main()

    assert exit_info.value.code == 1
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err + caplog.text
    assert "TELEGRAM_BOT_TOKEN" in caplog.text


def test_config_error_exits_cleanly(monkeypatch, caplog):
    def fail():
        raise ConfigError("설정에서 참조한 환경변수가 없습니다: telegram.bot_token → TELEGRAM_BOT_TOKEN")

    monkeypatch.setattr(app_main, "load_settings", fail)

    with pytest.raises(SystemExit) as exit_info:
        app_main.main()

    assert exit_info.value.code == 1
    assert "TELEGRAM_BOT_TOKEN" in caplog.text


async def test_system_jobs_include_the_weekly_plan(scheduler):
    settings = SimpleNamespace(briefing=BriefingSettings())
    app_main._add_system_jobs(scheduler, settings, dispatcher=None, assistant=None, briefing=None)

    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == {
        "system:release",
        "system:compact",
        "system:briefing:morning",
        "system:briefing:evening",
        "system:briefing:weekly",
    }
    weekly = str(jobs["system:briefing:weekly"].trigger)
    assert "day_of_week='sun'" in weekly and "hour='21'" in weekly
    assert "hour='7'" in str(jobs["system:briefing:morning"].trigger)


def runtime_settings(tmp_path) -> Settings:
    return Settings(
        telegram=TelegramSettings(bot_token="123:abc", allowed_user_id=1),
        notification=NotificationSettings(),
        storage=StorageSettings(
            db_path=tmp_path / "private" / "assistant.db",
            memory_path=tmp_path / "private" / "memory.md",
            profile_path=tmp_path / "private" / "profile.md",
            system_prompt_path=Path("prompts/system_prompt.md"),
        ),
        llm=LLMSettings(),
        weather=WeatherSettings(),
        conversation=ConversationSettings(),
        briefing=BriefingSettings(),
    )


async def test_runtime_registers_every_tool_once(tmp_path):
    from tests.llm.fake_provider import create as fake_llm

    llm = await fake_llm(LLMSettings())
    runtime = await app_main.create_runtime(runtime_settings(tmp_path), bot=object(), llm=llm)
    try:
        names = [tool["name"] for tool in runtime.services.registry.definitions()]
        assert names == sorted(names) and len(names) == len(set(names))
        assert set(names) == {
            "add_todo", "list_todos", "update_todo", "complete_todo", "delete_todo",
            "remember", "forget",
            "create_reminder", "create_scheduled_task", "list_scheduled_tasks", "pause_scheduled_task",
            "resume_scheduled_task", "cancel_scheduled_task",
            "get_weather", "web_search",
            "save_link", "save_note", "search_archive", "open_archive_item", "delete_archive_item",
        }
    finally:
        await runtime.close()
