import pytest
from telegram.error import InvalidToken

from app import main as app_main
from app.core.config import ConfigError

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
