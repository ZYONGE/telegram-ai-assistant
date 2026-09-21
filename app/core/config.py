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
from datetime import time, timedelta
from enum import StrEnum
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
    # 수집 실패가 같은 원인으로 이어질 때 몇 시간마다 다시 알릴지. 0이면 한 번만 알린다.
    # 같은 소식을 되풀이하지 않는 것이 기본이지만, 며칠째 수집이 안 되는 것은 계속 알려야 한다.
    failure_repeat_hours: int = 6
    # 할 일 마감 전 알림 시점 (사용자 지시 2026-09-21). 끝낸 일은 알리지 않는다.
    deadline_reminders: tuple[timedelta, ...] = (
        timedelta(days=7),
        timedelta(days=4),
        timedelta(days=1),
        timedelta(hours=12),
        timedelta(hours=3),
        timedelta(hours=1),
    )


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    """로그를 어디에 어떻게 남길지.

    화면(표준 출력)에는 늘 남긴다. 도커가 그것을 받아 간다.
    파일은 컨테이너를 다시 만들어도 남으므로 지난 일을 되짚을 때 쓴다.
    수집한 내용이 섞일 수 있어 **개인 파일로 보고 private/ 안에 둔다** (절대 규칙 12).
    """

    # 비워 두면 파일로 남기지 않는다. 켜는 것은 config.toml에서 정한다.
    file: Path | None = None
    # 이 크기를 넘으면 새 파일로 넘어간다 (MB)
    max_mb: int = 10
    # 지난 파일을 몇 개까지 두는지
    backups: int = 5


@dataclass(frozen=True, slots=True)
class StorageSettings:
    db_path: Path
    memory_path: Path
    profile_path: Path
    system_prompt_path: Path
    # 사용자가 직접 쓰는 판단 기준 (말투, 보고 방법, 일정·메일 처리 방침)
    instructions_path: Path = PRIVATE_DIR / "instructions.md"


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
class GoogleAccountSettings:
    """연결할 Google 계정 하나. 계정 주소는 저장하지 않고 토큰 파일로만 구분한다."""

    label: str
    token_file: Path
    calendar_id: str = "primary"
    # 새 일정을 넣을 기본 계정
    default: bool = False


@dataclass(frozen=True, slots=True)
class GoogleSettings:
    """Google 계정 연동(캘린더·Gmail). 클라이언트 파일과 토큰은 private/ 안에만 둔다.

    계정 여러 개를 한 비서가 함께 본다. OAuth 클라이언트 파일 하나를 모든 계정이 공유하고,
    로그인만 계정 수만큼 한 번씩 한다.
    """

    client_file: Path = PRIVATE_DIR / "google_client.json"
    accounts: tuple[GoogleAccountSettings, ...] = ()
    # 비우면 app/google/auth.py의 기본 범위(캘린더 + gmail.modify)를 쓴다
    scopes: tuple[str, ...] = ()


# 이 낱말이 공지에 있으면 즉시 알린다. 수업이 사라지거나 옮겨지는 일들이다
# (판정은 app/collectors/eclass/urgent.py).
DEFAULT_URGENT_WORDS = (
    "휴강",
    "보강",
    "결강",
    "강의실 변경",
    "장소 변경",
    "시간 변경",
    "일정 변경",
    "시험 변경",
    "연기",
    "취소",
    "긴급",
)


class Level(StrEnum):
    """eClass 화면 하나를 어떻게 다룰지 (app/collectors/eclass/scope.py)."""

    NOTIFY = "notify"  # 알림 게이트를 거쳐 알린다 (급하면 즉시)
    BRIEF = "brief"  # 브리핑에만 넣는다
    STORE = "store"  # 저장만 하고, 물어보면 답한다
    OFF = "off"  # 건드리지 않는다


@dataclass(frozen=True, slots=True)
class ScopePolicy:
    """무엇을 어느 수준으로 볼지. 낱말 목록이라 개인을 알아볼 수 있는 값이 없다."""

    unknown: Level = Level.OFF
    notify_paths: tuple[str, ...] = ()
    store_paths: tuple[str, ...] = ()
    off_paths: tuple[str, ...] = ()
    notify_words: tuple[str, ...] = ()
    store_words: tuple[str, ...] = ()
    off_words: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EclassSettings:
    """학교 eClass 수집 설정.

    주소는 학교를 특정하므로 private/local.toml에, 계정은 private/.env에 둔다.
    비밀번호는 수집기 안에서만 쓰고 모델 프롬프트·도구 결과·로그에 넣지 않는다 (절대 규칙 7).
    """

    # eClass 주소만 있으면 된다. 포털 SSO를 거치는 학교라면 portal_url도 채운다.
    eclass_url: str = ""
    portal_url: str = ""
    username: str = ""
    password: str = ""
    # 수집 간격(분). 0이면 수집하지 않는다.
    poll_minutes: int = 90
    # 이 시간 넘게 수집이 성공하지 못하면 알린다
    stale_hours: int = 12
    # 로그인 세션 저장 위치 (재로그인 횟수를 줄인다)
    session_file: Path = PRIVATE_DIR / "browser" / "eclass_session.json"
    # 탐색기가 만든 화면 목록과, 화면마다 정해 둔 처리 수준 (둘 다 개인 파일)
    catalog_file: Path = PRIVATE_DIR / "eclass_catalog.json"
    scope_file: Path = PRIVATE_DIR / "eclass_scope.json"
    # 어떤 화면을 어떻게 다룰지 정하는 규칙
    scope: ScopePolicy = ScopePolicy()
    # 이 낱말이 공지에 있으면 즉시 알린다 (app/collectors/eclass/urgent.py)
    urgent_words: tuple[str, ...] = DEFAULT_URGENT_WORDS
    # 브라우저를 띄울 때 넘길 인자. 컨테이너 안에서는 크로미움 자체 격리가 막혀
    # --no-sandbox가 필요할 수 있다. 기기마다 달라 private/local.toml에 둔다.
    browser_args: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.eclass_url and self.username and self.password and self.poll_minutes > 0)


@dataclass(frozen=True, slots=True)
class BackupSettings:
    """야간 백업. 잃으면 되돌릴 수 없는 것만 뜬다 (app/storage/backup.py)."""

    # 매일 이 시각에 뜬다. 조용한 시간이라 수집과 겹치지 않는다.
    at: time = time(3, 30)
    # 이 일수만큼만 보관한다. 0이면 지우지 않는다.
    keep_days: int = 14
    # 개인 파일이므로 private/ 안에 둔다
    directory: Path = PRIVATE_DIR / "backups"
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class MailSettings:
    """메일 수집 주기와 보호 도메인. 학교 도메인처럼 개인을 알아볼 수 있는 값은 private/local.toml에 둔다."""

    # 수집 간격(분). 0이면 메일 수집을 하지 않는다.
    poll_minutes: int = 10
    # 이 도메인에서 온 메일은 어떤 규칙에서도 휴지통으로 보내지 않는다
    protected_domains: tuple[str, ...] = ()


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
    # 텔레그램으로 보낸 위치를 기준으로 삼는다 (실시간 공유 중에는 자동 갱신)
    follow_telegram_location: bool = True
    # 받은 위치를 이 시간까지만 쓴다. 0이면 만료 없이 계속 쓴다 (기본값).
    location_ttl_hours: int = 0
    # 이 시간 안에 받은 위치는 "현재 위치", 그보다 오래되면 "마지막 위치"로 표시한다
    location_recent_hours: int = 6


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
    google: GoogleSettings = GoogleSettings()
    eclass: EclassSettings = EclassSettings()
    backup: BackupSettings = BackupSettings()
    logging: LoggingSettings = LoggingSettings()
    mail: MailSettings = MailSettings()
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
        google = data.get("google", {})
        eclass = data.get("eclass", {})
        backup = data.get("backup", {})
        logs = data.get("logging", {})
        mail = data.get("mail", {})
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
                failure_repeat_hours=int(
                    notification.get("failure_repeat_hours", n_default.failure_repeat_hours)
                ),
                deadline_reminders=(
                    parse_durations(notification["deadline_reminders"])
                    if "deadline_reminders" in notification
                    else n_default.deadline_reminders
                ),
            ),
            storage=StorageSettings(
                db_path=path(storage["db_path"]),
                memory_path=path(storage.get("memory_path", str(PRIVATE_DIR / "memory.md"))),
                profile_path=path(storage.get("profile_path", str(PRIVATE_DIR / "profile.md"))),
                system_prompt_path=path(storage.get("system_prompt_path", "prompts/system_prompt.md")),
                instructions_path=path(storage.get("instructions_path", str(PRIVATE_DIR / "instructions.md"))),
            ),
            llm=LLMSettings(
                provider=provider,
                chat_model=llm.get("chat_model", l_default.chat_model),
                light_model=llm.get("light_model", l_default.light_model),
                options=dict(llm.get(provider, {})),
            ),
            google=_google(google, path),
            eclass=EclassSettings(
                portal_url=str(eclass.get("portal_url", "")),
                eclass_url=str(eclass.get("eclass_url", "")),
                # 계정은 설정 파일이 아니라 환경변수에서만 읽는다
                username=str(env.get("ECLASS_ID", "")),
                password=str(env.get("ECLASS_PASSWORD", "")),
                poll_minutes=int(eclass.get("poll_minutes", 90)),
                stale_hours=int(eclass.get("stale_hours", 12)),
                session_file=path(eclass.get("session_file", str(PRIVATE_DIR / "browser" / "eclass_session.json"))),
                catalog_file=path(eclass.get("catalog_file", str(PRIVATE_DIR / "eclass_catalog.json"))),
                scope_file=path(eclass.get("scope_file", str(PRIVATE_DIR / "eclass_scope.json"))),
                scope=_scope(eclass.get("scope", {})),
                urgent_words=tuple(str(word) for word in eclass.get("urgent_words", DEFAULT_URGENT_WORDS)),
                browser_args=tuple(str(arg) for arg in eclass.get("browser_args", ())),
            ),
            logging=LoggingSettings(
                file=path(logs["file"]) if logs.get("file") else None,
                max_mb=int(logs.get("max_mb", 10)),
                backups=int(logs.get("backups", 5)),
            ),
            backup=BackupSettings(
                at=_time(backup, "at", BackupSettings().at),
                keep_days=int(backup.get("keep_days", 14)),
                directory=path(backup.get("directory", str(PRIVATE_DIR / "backups"))),
                enabled=bool(backup.get("enabled", True)),
            ),
            mail=MailSettings(
                poll_minutes=int(mail.get("poll_minutes", 10)),
                protected_domains=tuple(str(item).lower() for item in mail.get("protected_domains", ())),
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


def _scope(section: Mapping[str, Any]) -> ScopePolicy:
    """eClass 화면을 어떻게 다룰지 정하는 규칙. 낱말 목록이라 개인정보가 없다."""
    default = ScopePolicy()

    def words(key: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
        value = section.get(key)
        if value is None:
            return fallback
        return tuple(str(item) for item in value)

    try:
        unknown = Level(str(section.get("unknown", default.unknown)))
    except ValueError as exc:
        raise ConfigError(
            f"[eclass.scope] unknown은 {', '.join(str(level) for level in Level)} 중 하나여야 합니다."
        ) from exc

    return ScopePolicy(
        unknown=unknown,
        notify_paths=words("notify_paths", default.notify_paths),
        store_paths=words("store_paths", default.store_paths),
        off_paths=words("off_paths", default.off_paths),
        notify_words=words("notify_words", default.notify_words),
        store_words=words("store_words", default.store_words),
        off_words=words("off_words", default.off_words),
    )


def _google(section: Mapping[str, Any], path: Any) -> GoogleSettings:
    """계정 목록을 읽는다. 목록이 없으면 계정 하나(기본)로 본다."""
    raw_accounts = section.get("accounts") or [{"label": "기본"}]
    accounts = []
    for index, entry in enumerate(raw_accounts, start=1):
        label = str(entry.get("label", f"계정{index}")).strip() or f"계정{index}"
        accounts.append(
            GoogleAccountSettings(
                label=label,
                token_file=path(entry.get("token_file", str(PRIVATE_DIR / f"google_token_{index}.json"))),
                calendar_id=str(entry.get("calendar_id", "primary")),
                default=bool(entry.get("default", False)),
            )
        )
    if not any(account.default for account in accounts):
        first = accounts[0]
        accounts[0] = GoogleAccountSettings(first.label, first.token_file, first.calendar_id, default=True)
    labels = [account.label for account in accounts]
    if len(set(labels)) != len(labels):
        raise ConfigError(f"[[google.accounts]]의 label이 겹칩니다: {labels}")
    return GoogleSettings(
        client_file=path(section.get("client_file", str(PRIVATE_DIR / "google_client.json"))),
        accounts=tuple(accounts),
        scopes=tuple(section.get("scopes", ())),
    )


def _weather(section: Mapping[str, Any], env: Mapping[str, str]) -> WeatherSettings:
    """날씨 설정. 키는 선택 항목이라 없으면 기능만 끄고 오류를 내지 않는다."""
    return WeatherSettings(
        api_key=str(section.get("api_key") or env.get("WEATHER_API_KEY", "")),
        nx=int(section.get("nx", 0)),
        ny=int(section.get("ny", 0)),
        lat=float(section["lat"]) if "lat" in section else None,
        lon=float(section["lon"]) if "lon" in section else None,
        place=str(section.get("place", "")),
        follow_telegram_location=bool(section.get("follow_telegram_location", True)),
        location_ttl_hours=int(section.get("location_ttl_hours", 0)),
        location_recent_hours=int(section.get("location_recent_hours", 6)),
    )


def parse_durations(values: list[str] | tuple[str, ...]) -> tuple[timedelta, ...]:
    """["7d", "12h", "30m"] → 시간 간격. 알아볼 수 없는 값이 있으면 설정 오류로 본다."""
    units = {"d": "days", "h": "hours", "m": "minutes"}
    durations = []
    for value in values:
        text = str(value).strip().lower()
        if len(text) < 2 or text[-1] not in units or not text[:-1].isdigit() or int(text[:-1]) <= 0:
            raise ConfigError(f"시간 간격을 읽지 못했습니다: {value!r} (예: 7d, 12h, 30m)")
        durations.append(timedelta(**{units[text[-1]]: int(text[:-1])}))
    return tuple(durations)
