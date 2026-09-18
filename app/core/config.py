"""설정 로드: config.toml + private/local.toml + private/.env.

개인정보와 비밀값은 git에서 제외된 `private/` 폴더 하나에만 둔다.
- 비밀값은 config.toml에 직접 쓰지 않고 `${ENV_VAR}`로만 참조한다 (값은 private/.env).
- 개인을 알아볼 수 있는 설정값(지역, 주소 등)은 private/local.toml에 두고, 있으면 config.toml 위에 덮어쓴다.
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

# 개인정보·비밀값이 모이는 폴더 (git 제외). 경로는 config.toml 위치 기준이다.
PRIVATE_DIR = Path("private")
ENV_FILE = PRIVATE_DIR / ".env"
LOCAL_CONFIG = PRIVATE_DIR / "local.toml"


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
class WeatherSettings:
    """기상청 단기예보 설정. 키는 private/.env, 동네 좌표는 private/local.toml에 둔다.

    좌표는 격자(nx, ny)를 바로 적거나 위경도(lat, lon)를 적으면 수집기가 격자로 바꾼다.
    키나 좌표가 없으면 날씨 기능만 꺼지고 비서는 그대로 동작한다.
    """

    api_key: str = ""
    nx: int = 0
    ny: int = 0
    lat: float | None = None
    lon: float | None = None
    # 브리핑에 붙일 지역 이름. 비워 두면 표시하지 않는다.
    place: str = ""


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
    # 주간 계획은 일요일 이 시각에 보낸다
    weekly: time = time(21, 0)


@dataclass(frozen=True, slots=True)
class Settings:
    telegram: TelegramSettings
    notification: NotificationSettings
    storage: StorageSettings
    llm: LLMSettings = field(default_factory=LLMSettings)
    weather: WeatherSettings = WeatherSettings()
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


def merge_settings(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """override에 있는 항목만 base 위에 덮어쓴다 (표 안쪽까지)."""
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = merge_settings(current, value)
        else:
            merged[key] = value
    return merged


def load_settings(
    config_path: Path = Path("config.toml"),
    env_file: Path | None = None,
    env: Mapping[str, str] | None = None,
    local_path: Path | None = None,
) -> Settings:
    base = config_path.parent
    if env is None:
        load_dotenv(env_file or base / ENV_FILE, override=False)
        env = os.environ
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"설정 파일이 없습니다: {config_path}") from exc

    local = local_path if local_path is not None else base / LOCAL_CONFIG
    if local.exists():
        try:
            raw = merge_settings(raw, tomllib.loads(local.read_text(encoding="utf-8")))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"개인 설정 파일 형식이 잘못되었습니다: {local} ({exc})") from exc

    data = resolve_env_refs(raw, env)

    def path(value: str) -> Path:
        candidate = Path(value)
        return candidate if candidate.is_absolute() else base / candidate

    try:
        telegram = data["telegram"]
        notification = data.get("notification", {})
        storage = data["storage"]
        llm = data.get("llm", {})
        weather = data.get("weather", {})
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
                memory_path=path(storage.get("memory_path", str(PRIVATE_DIR / "memory.md"))),
                profile_path=path(storage.get("profile_path", str(PRIVATE_DIR / "profile.md"))),
                system_prompt_path=path(storage.get("system_prompt_path", "prompts/system_prompt.md")),
            ),
            llm=LLMSettings(
                provider=provider,
                chat_model=llm.get("chat_model", l_default.chat_model),
                light_model=llm.get("light_model", l_default.light_model),
                options=dict(llm.get(provider, {})),
            ),
            weather=_weather(weather, env),
            conversation=ConversationSettings(
                idle_compact_minutes=int(conversation.get("idle_compact_minutes", c_default.idle_compact_minutes)),
                max_active_messages=int(conversation.get("max_active_messages", c_default.max_active_messages)),
            ),
            briefing=BriefingSettings(
                morning=_time(briefing, "morning", b_default.morning),
                evening=_time(briefing, "evening", b_default.evening),
                weekly=_time(briefing, "weekly", b_default.weekly),
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"설정 형식이 잘못되었습니다: {exc!r}") from exc


def _time(section: Mapping[str, Any], key: str, default: time) -> time:
    return time.fromisoformat(section[key]) if key in section else default


def _weather(section: Mapping[str, Any], env: Mapping[str, str]) -> WeatherSettings:
    """날씨 설정. 키는 선택 항목이라 없으면 기능만 끄고 오류를 내지 않는다."""
    return WeatherSettings(
        api_key=str(section.get("api_key") or env.get("WEATHER_API_KEY", "")),
        nx=int(section.get("nx", 0)),
        ny=int(section.get("ny", 0)),
        lat=float(section["lat"]) if "lat" in section else None,
        lon=float(section["lon"]) if "lon" in section else None,
        place=str(section.get("place", "")),
    )
