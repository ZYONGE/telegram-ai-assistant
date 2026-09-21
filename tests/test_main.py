import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.error import InvalidToken

from app import main as app_main
from app.main import setup_logging
from app.core.config import (
    BriefingSettings,
    ConfigError,
    ConversationSettings,
    EclassSettings,
    LLMSettings,
    LoggingSettings,
    MailSettings,
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
    monkeypatch.setattr(app_main, "load_settings", lambda: SimpleNamespace(logging=LoggingSettings()))
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


# --- 수집기 등록: 메일과 eClass는 서로 독립이어야 한다 (docs/tasks.md T-02) ---

ECLASS_ON = EclassSettings(
    eclass_url="https://eclass.example.ac.kr/", username="학번", password="비밀", poll_minutes=90
)
ECLASS_OFF = EclassSettings()


def collector_settings(mail_minutes: int, eclass: EclassSettings) -> SimpleNamespace:
    return SimpleNamespace(
        mail=MailSettings(poll_minutes=mail_minutes),
        eclass=eclass,
        notification=NotificationSettings(),
    )


def collector_jobs(scheduler, *, mail_minutes: int, google_ready: bool, eclass: EclassSettings) -> set[str]:
    app_main._add_collector_jobs(
        scheduler,
        collector_settings(mail_minutes, eclass),
        ingestor=None,
        mail_collector=None,
        google=SimpleNamespace(ready=google_ready),
        eclass_collector=object() if eclass.enabled else None,
    )
    return {job.id for job in scheduler.get_jobs()}


async def test_both_collectors_are_registered(scheduler):
    jobs = collector_jobs(scheduler, mail_minutes=10, google_ready=True, eclass=ECLASS_ON)
    assert jobs == {"collector:mail", "collector:eclass"}


async def test_eclass_runs_without_a_google_account(scheduler):
    """Google을 연결하지 않아도 eClass는 돌아야 한다. 예전에는 함께 꺼졌다."""
    jobs = collector_jobs(scheduler, mail_minutes=10, google_ready=False, eclass=ECLASS_ON)
    assert jobs == {"collector:eclass"}


async def test_eclass_runs_with_mail_collection_turned_off(scheduler):
    jobs = collector_jobs(scheduler, mail_minutes=0, google_ready=True, eclass=ECLASS_ON)
    assert jobs == {"collector:eclass"}


async def test_mail_runs_without_eclass(scheduler):
    jobs = collector_jobs(scheduler, mail_minutes=10, google_ready=True, eclass=ECLASS_OFF)
    assert jobs == {"collector:mail"}


async def test_neither_is_registered_when_both_are_off(scheduler):
    jobs = collector_jobs(scheduler, mail_minutes=0, google_ready=False, eclass=ECLASS_OFF)
    assert jobs == set()


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
            "list_events", "find_free_time", "add_event", "update_event", "delete_event",
            "list_mail_rules", "add_mail_rule", "delete_mail_rule",
            "list_waiting_replies", "resolve_waiting_reply", "draft_reply", "list_recent_mail",
            "search_mail", "read_mail", "list_mail_labels", "organize_mail", "trash_mail", "spam_mail",
            "draft_mail_reply", "draft_new_mail",
            "save_link", "save_note", "search_archive", "open_archive_item", "delete_archive_item",
        }
    finally:
        await runtime.close()


# --- 로그 ---


def test_logs_go_to_a_rotating_file(tmp_path):
    target = tmp_path / "logs" / "assistant.log"
    setup_logging(LoggingSettings(file=target, max_mb=1, backups=3))
    try:
        logging.getLogger("app.test").warning("한 줄 남깁니다")
        assert target.exists()
        assert "한 줄 남깁니다" in target.read_text(encoding="utf-8")
        handler = logging.getLogger().handlers[-1]
        assert handler.maxBytes == 1024 * 1024 and handler.backupCount == 3
    finally:
        _drop_file_handlers()


def test_no_file_means_screen_only(tmp_path):
    before = len(logging.getLogger().handlers)
    setup_logging(LoggingSettings(file=None))
    assert len(logging.getLogger().handlers) == before


def test_a_log_file_we_cannot_open_does_not_stop_the_assistant(tmp_path):
    """파일에 못 남긴다고 비서가 안 뜰 까닭은 없다."""
    blocker = tmp_path / "막힘"
    blocker.write_text("파일이라 폴더를 만들 수 없다", encoding="utf-8")
    before = len(logging.getLogger().handlers)

    setup_logging(LoggingSettings(file=blocker / "assistant.log"))
    assert len(logging.getLogger().handlers) == before


def _drop_file_handlers() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, RotatingFileHandler):
            handler.close()
            root.removeHandler(handler)


# --- 텔레그램 오류 (docs/tasks.md T-28) ---


async def test_a_network_error_is_one_line_not_a_stack_trace(caplog):
    """인터넷이 끊기면 몇 초마다 재시도한다. 그때마다 스택을 찍으면 로그가 가득 찬다."""
    from telegram.error import NetworkError

    context = SimpleNamespace(error=NetworkError("httpx.ConnectError: 이름을 찾지 못했습니다"))
    with caplog.at_level(logging.WARNING):
        await app_main.on_telegram_error(None, context)

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelname == "WARNING" and record.exc_info is None
    assert "다시 붙습니다" in record.message


async def test_a_real_error_keeps_its_stack_trace(caplog):
    context = SimpleNamespace(error=ValueError("무언가 잘못됐다"))
    with caplog.at_level(logging.ERROR):
        await app_main.on_telegram_error(None, context)

    assert caplog.records[0].levelname == "ERROR"
    assert caplog.records[0].exc_info is not None


def test_the_error_handler_is_registered():
    """걸어 두지 않으면 텔레그램이 '처리기가 없다'며 스택을 통째로 찍는다."""
    import inspect

    source = inspect.getsource(app_main.build_application)
    assert "add_error_handler(on_telegram_error)" in source
