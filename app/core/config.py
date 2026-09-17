"""설정 로드: config.toml + .env.

비밀값은 config.toml에 직접 쓰지 않고 `${ENV_VAR}`로만 참조한다.
참조한 환경변수가 없으면 빠진 것을 모두 모아 한 번에 알린다 (값은 메시지에 넣지 않는다).
"""

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class TelegramSettings:
    bot_token: str
    allowed_user_id: int


@dataclass(frozen=True, slots=True)
class NotificationSettings:
    quiet_start: time = time(23, 0)
    quiet_end: time = time(6, 30)
    # 브리핑을 제외한 선제 알림의 하루 최대 건수 (사용자가 요청한 리마인더는 세지 않음)
    daily_limit: int = 5


@dataclass(frozen=True, slots=True)
class StorageSettings:
    db_path: Path


@dataclass(frozen=True, slots=True)
class Settings:
    telegram: TelegramSettings
    notification: NotificationSettings
    storage: StorageSettings


def resolve_env_refs(data: Any, env: Mapping[str, str]) -> Any:
    missing: list[str] = []

    def walk(value: Any, path: str) -> Any:
        if isinstance(value, str):
            def substitute(match: re.Match[str]) -> str:
                name = match.group(1)
                if name not in env:
                    missing.append(f"{path} → {name}")
                    return match.group(0)
                return env[name]

            return _ENV_REF.sub(substitute, value)
        if isinstance(value, dict):
            return {key: walk(item, f"{path}.{key}" if path else key) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item, f"{path}[{index}]") for index, item in enumerate(value)]
        return value

    resolved = walk(data, "")
    if missing:
        raise ConfigError("설정에서 참조한 환경변수가 없습니다: " + ", ".join(missing))
    return resolved


def load_settings(
    config_path: Path = Path("config.toml"),
    env_file: Path = Path(".env"),
    env: Mapping[str, str] | None = None,
) -> Settings:
    if env is None:
        load_dotenv(env_file, override=False)
        env = os.environ
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"설정 파일이 없습니다: {config_path}") from exc

    data = resolve_env_refs(raw, env)
    try:
        telegram = data["telegram"]
        notification = data.get("notification", {})
        db_path = Path(data["storage"]["db_path"])
        defaults = NotificationSettings()
        return Settings(
            telegram=TelegramSettings(
                bot_token=telegram["bot_token"],
                allowed_user_id=int(telegram["allowed_user_id"]),
            ),
            notification=NotificationSettings(
                quiet_start=time.fromisoformat(notification.get("quiet_start", defaults.quiet_start.isoformat())),
                quiet_end=time.fromisoformat(notification.get("quiet_end", defaults.quiet_end.isoformat())),
                daily_limit=int(notification.get("daily_limit", defaults.daily_limit)),
            ),
            storage=StorageSettings(
                db_path=db_path if db_path.is_absolute() else config_path.parent / db_path,
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"설정 형식이 잘못되었습니다: {exc!r}") from exc
