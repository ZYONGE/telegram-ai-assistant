"""설정 로드: config.toml + .env.

비밀값은 config.toml에 직접 쓰지 않고 `${ENV_VAR}`로만 참조한다.
참조한 환경변수가 없으면 빠진 것을 모두 모아 한 번에 알린다 (값은 메시지에 넣지 않는다).
"""

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
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
    memory_path: Path
    profile_path: Path
    system_prompt_path: Path


@dataclass(frozen=True, slots=True)
class LLMSettings:
    """모델 호출 설정. 제공사 어댑터는 app/llm/에 있다."""

    provider: str = "gemini"
    # 대화·판단
    chat_model: str = "gemini-3.5-flash-lite"
    # 브리핑 문장 다듬기, 대화 요약
    light_model: str = "gemini-3.5-flash-lite"
    # 제공사별 설정 (config.toml의 [llm.<provider>] 표)
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConversationSettings:
    # 마지막 대화 후 이 시간이 지나면 대화를 요약해 압축한다
    idle_compact_minutes: int = 30
    # 요약 전 대화가 이보다 길어지면 다음 메시지 전에 압축한다
    max_active_messages: int = 80


@dataclass(frozen=True, slots=True)
class BriefingSettings:
    morning: time = time(7, 0)
    evening: time = time(22, 0)


@dataclass(frozen=True, slots=True)
class Settings:
    telegram: TelegramSettings
    notification: NotificationSettings
    storage: StorageSettings
    llm: LLMSettings = field(default_factory=LLMSettings)
    conversation: ConversationSettings = ConversationSettings()
    briefing: BriefingSettings = BriefingSettings()


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
    base = config_path.parent

    def path(value: str) -> Path:
        candidate = Path(value)
        return candidate if candidate.is_absolute() else base / candidate

    try:
        telegram = data["telegram"]
        notification = data.get("notification", {})
        storage = data["storage"]
        llm = data.get("llm", {})
        conversation = data.get("conversation", {})
        briefing = data.get("briefing", {})
        n_default, l_default = NotificationSettings(), LLMSettings()
        provider = llm.get("provider", l_default.provider)
        c_default, b_default = ConversationSettings(), BriefingSettings()
        return Settings(
            telegram=TelegramSettings(
                bot_token=telegram["bot_token"],
                allowed_user_id=int(telegram["allowed_user_id"]),
            ),
            notification=NotificationSettings(
                quiet_start=_time(notification, "quiet_start", n_default.quiet_start),
                quiet_end=_time(notification, "quiet_end", n_default.quiet_end),
                daily_limit=int(notification.get("daily_limit", n_default.daily_limit)),
            ),
            storage=StorageSettings(
                db_path=path(storage["db_path"]),
                memory_path=path(storage.get("memory_path", "data/memory.md")),
                profile_path=path(storage.get("profile_path", "data/profile.md")),
                system_prompt_path=path(storage.get("system_prompt_path", "prompts/system_prompt.md")),
            ),
            llm=LLMSettings(
                provider=provider,
                chat_model=llm.get("chat_model", l_default.chat_model),
                light_model=llm.get("light_model", l_default.light_model),
                options=dict(llm.get(provider, {})),
            ),
            conversation=ConversationSettings(
                idle_compact_minutes=int(conversation.get("idle_compact_minutes", c_default.idle_compact_minutes)),
                max_active_messages=int(conversation.get("max_active_messages", c_default.max_active_messages)),
            ),
            briefing=BriefingSettings(
                morning=_time(briefing, "morning", b_default.morning),
                evening=_time(briefing, "evening", b_default.evening),
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"설정 형식이 잘못되었습니다: {exc!r}") from exc


def _time(section: Mapping[str, Any], key: str, default: time) -> time:
    return time.fromisoformat(section[key]) if key in section else default
