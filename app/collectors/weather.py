"""기상청 단기예보 조회와 옷차림 기본안.

- 예보 요청은 이 모듈에서만 한다. 모델에는 정리된 문장만 넘어간다.
- 옷차림 기본안은 코드의 기온 구간표로 정한다 (CLAUDE.md 6절). 모델은 문장만 다듬는다.
- 실패는 WeatherUnavailable로 알린다. 예외 메시지에 인증키가 섞이지 않도록 원문을 그대로 쓰지 않는다.
"""

import logging
import math
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Protocol

import httpx

from app.core.clock import to_kst
from app.core.config import WeatherSettings
from app.storage.location import StoredLocation

logger = logging.getLogger(__name__)

ENDPOINT = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
# 단기예보 발표 시각 (KST). 발표 직후에는 아직 자료가 없어 조금 기다렸다 이전 발표본을 쓴다.
BASE_HOURS = (2, 5, 8, 11, 14, 17, 20, 23)
PUBLISH_DELAY = timedelta(minutes=15)
# 예보는 3일치라 하루 약 290건이다. 오늘·내일을 덮으려면 1000건이면 넉넉하다.
MAX_ROWS = 1000

SKY_NAMES = {"1": "맑음", "3": "구름많음", "4": "흐림"}
RAIN_NAMES = {"1": "비", "2": "비/눈", "3": "눈", "4": "소나기", "5": "빗방울", "6": "진눈깨비", "7": "눈날림"}

# 하루 중 살펴볼 시간대 (CLAUDE.md 6절: 아침 07~09, 점심 12, 저녁 18~21)
SLOTS = (("아침", (7, 8, 9)), ("점심", (12, 13)), ("저녁", (18, 19, 20, 21)))

# 기온 구간별 옷차림 기본안. 위에서부터 기준 기온 이상이면 그 문구를 쓴다.
CLOTHING = (
    (28, "민소매나 반팔처럼 얇고 통풍 잘 되는 옷"),
    (23, "반팔이나 얇은 셔츠"),
    (20, "긴팔 티셔츠나 얇은 가디건"),
    (17, "얇은 니트, 맨투맨, 가디건"),
    (12, "자켓이나 야상, 두꺼운 가디건"),
    (9, "트렌치코트나 점퍼"),
    (5, "코트나 기모 옷"),
    (-50, "패딩과 두꺼운 외투, 목도리"),
)
# 일교차가 이 이상이면 겉옷을 권한다
BIG_GAP = 8.0
# 강수확률이 이 이상이면 우산을 권한다
UMBRELLA_CHANCE = 60

DISABLED_MESSAGE = (
    "날씨 설정이 아직 없습니다. private/.env에 WEATHER_API_KEY를 넣고 "
    "private/local.toml에 동네 좌표를 적어 주세요."
)
NO_LOCATION_MESSAGE = (
    "어디 날씨를 볼지 아직 모릅니다. 텔레그램에서 클립(첨부) → 위치를 눌러 지금 위치를 보내 주세요. "
    "실시간 위치 공유를 켜시면 이동할 때마다 자동으로 따라갑니다."
)
# 위치를 기준으로 볼 때 브리핑에 붙는 이름
LIVE_PLACE = "현재 위치"
STALE_PLACE = "마지막 위치"


class LocationSource(Protocol):
    """마지막으로 받은 위치를 돌려준다 (app/storage/location.py)."""

    async def latest(self) -> StoredLocation | None: ...


class WeatherUnavailable(Exception):
    """예보를 가져오지 못했다. 비서는 계속 동작하고 사용자에게는 짧게만 알린다."""


@dataclass(frozen=True, slots=True)
class SlotForecast:
    name: str
    temp: float | None
    sky: str
    rain: str
    rain_chance: int

    def render(self) -> str:
        if self.temp is None:
            return f"{self.name} 예보 없음"
        parts = [f"{self.name} {self.temp:.0f}도", self.rain or self.sky]
        if self.rain_chance >= 30:
            parts.append(f"강수 {self.rain_chance}%")
        return " ".join(part for part in parts if part)


@dataclass(frozen=True, slots=True)
class DayForecast:
    day: date
    place: str
    slots: tuple[SlotForecast, ...]
    low: float | None
    high: float | None
    # 사용자가 더위를 타는 정도 (설정 feels_warmer). 옷차림 구간을 이만큼 따뜻하게 본다.
    feels_warmer: float = 0.0
    umbrella_chance: int = UMBRELLA_CHANCE

    @property
    def temps(self) -> list[float]:
        return [slot.temp for slot in self.slots if slot.temp is not None]

    @property
    def gap(self) -> float | None:
        if self.low is not None and self.high is not None:
            return self.high - self.low
        temps = self.temps
        return max(temps) - min(temps) if len(temps) > 1 else None

    def render(self) -> str:
        head = f"{self.place} " if self.place else ""
        line = head + " · ".join(slot.render() for slot in self.slots)
        if self.low is not None and self.high is not None:
            line += f" (최저 {self.low:.0f}도 / 최고 {self.high:.0f}도)"
        return line

    def clothing(self) -> str:
        """코드가 정하는 옷차림 기본안. 모델은 이 문장을 다듬기만 한다."""
        temps = self.temps
        if not temps:
            return "옷차림: 기온 정보가 없어 기본안을 내지 못했습니다."
        basis = min(temps) + self.feels_warmer
        advice = next(text for limit, text in CLOTHING if basis >= limit)
        notes = [f"옷차림: {advice}"]
        gap = self.gap
        if gap is not None and gap >= BIG_GAP:
            notes.append(f"일교차가 {gap:.0f}도라 겉옷을 챙기세요.")
        wet = [slot for slot in self.slots if slot.rain or slot.rain_chance >= self.umbrella_chance]
        if wet:
            when = ", ".join(slot.name for slot in wet)
            notes.append(f"{when}에 비 소식이 있어 우산을 챙기세요.")
        return " ".join(notes)


def to_grid(lat: float, lon: float) -> tuple[int, int]:
    """위경도를 기상청 격자(nx, ny)로 바꾼다. 기상청이 공개한 람베르트 정각원뿔도법 식."""
    re, grid = 6371.00877, 5.0
    slat1, slat2 = math.radians(30.0), math.radians(60.0)
    olon, olat = math.radians(126.0), math.radians(38.0)
    xo, yo = 43, 136

    sn = math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5)
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(sn)
    sf = math.tan(math.pi * 0.25 + slat1 * 0.5) ** sn * math.cos(slat1) / sn
    ro = (re / grid) * sf / math.tan(math.pi * 0.25 + olat * 0.5) ** sn
    ra = (re / grid) * sf / math.tan(math.pi * 0.25 + math.radians(lat) * 0.5) ** sn

    theta = math.radians(lon) - olon
    theta = (theta + math.pi) % (2 * math.pi) - math.pi
    theta *= sn
    return int(ra * math.sin(theta) + xo + 0.5), int(ro - ra * math.cos(theta) + yo + 0.5)


def grid_of(settings: WeatherSettings) -> tuple[int, int]:
    """설정에서 격자 좌표를 정한다. nx·ny가 있으면 그대로, 없으면 위경도로 계산한다."""
    if settings.nx and settings.ny:
        return settings.nx, settings.ny
    if settings.lat is not None and settings.lon is not None:
        return to_grid(settings.lat, settings.lon)
    return 0, 0


def base_time(now: datetime) -> tuple[str, str]:
    local = to_kst(now) - PUBLISH_DELAY
    for hour in reversed(BASE_HOURS):
        if local.hour >= hour:
            return local.strftime("%Y%m%d"), f"{hour:02d}00"
    return (local - timedelta(days=1)).strftime("%Y%m%d"), "2300"


def _number(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_items(payload: object) -> list[dict]:
    """기상청 응답에서 예보 항목만 꺼낸다. 형식이 다르면 WeatherUnavailable."""
    try:
        response = payload["response"]
        if response["header"].get("resultCode") != "00":
            message = response["header"].get("resultMsg", "이유 없음")
            raise WeatherUnavailable(f"기상청이 요청을 처리하지 못했습니다 ({message}).")
        items = response["body"]["items"]["item"]
    except (KeyError, TypeError, IndexError) as exc:
        raise WeatherUnavailable("기상청 응답을 읽지 못했습니다.") from exc
    if not isinstance(items, list):
        raise WeatherUnavailable("기상청 응답을 읽지 못했습니다.")
    return items


def build_forecast(items: list[dict], day: date, place: str = "") -> DayForecast:
    target = day.strftime("%Y%m%d")
    by_hour: dict[int, dict[str, object]] = {}
    low = high = None
    for item in items:
        if item.get("fcstDate") != target:
            continue
        category, value = item.get("category"), item.get("fcstValue")
        try:
            hour = int(str(item.get("fcstTime", "")).zfill(4)[:2])
        except ValueError:
            continue
        by_hour.setdefault(hour, {})[category] = value
        if category == "TMN":
            low = _number(value)
        elif category == "TMX":
            high = _number(value)

    slots = tuple(_slot(name, hours, by_hour) for name, hours in SLOTS)
    temps = [slot.temp for slot in slots if slot.temp is not None]
    if not temps:
        raise WeatherUnavailable("그 날짜의 예보가 아직 없습니다.")
    return DayForecast(
        day=day,
        place=place,
        slots=slots,
        low=low if low is not None else min(temps),
        high=high if high is not None else max(temps),
    )


def _slot(name: str, hours: tuple[int, ...], by_hour: dict[int, dict[str, object]]) -> SlotForecast:
    for hour in hours:
        values = by_hour.get(hour)
        if values and "TMP" in values:
            return SlotForecast(
                name=name,
                temp=_number(values.get("TMP")),
                sky=SKY_NAMES.get(str(values.get("SKY")), ""),
                rain=RAIN_NAMES.get(str(values.get("PTY")), ""),
                rain_chance=int(_number(values.get("POP")) or 0),
            )
    return SlotForecast(name, None, "", "", 0)


class KmaWeather:
    """기상청 단기예보 수집기.

    기준 좌표는 (1) 텔레그램으로 받은 최근 위치, (2) 설정에 적어 둔 동네 순으로 고른다.
    좌표는 격자로 바꿔서만 쓰고, 로그나 오류 메시지에 남기지 않는다.
    """

    name = "weather"

    def __init__(
        self,
        settings: WeatherSettings,
        client: httpx.AsyncClient,
        endpoint: str = ENDPOINT,
        location: LocationSource | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._endpoint = endpoint
        self._grid = grid_of(settings)
        self._location = location if settings.follow_telegram_location else None
        # ttl이 0이면 만료 없이 마지막 위치를 계속 쓴다
        self._ttl = timedelta(hours=settings.location_ttl_hours) if settings.location_ttl_hours > 0 else None
        self._recent = timedelta(hours=max(settings.location_recent_hours, 0))

    @property
    def enabled(self) -> bool:
        return bool(self._settings.api_key) and (self._grid != (0, 0) or self._location is not None)

    async def where(self, now: datetime) -> tuple[tuple[int, int], str] | None:
        """이번 조회에 쓸 격자 좌표와 지역 이름. 기준이 없으면 None."""
        if self._location is not None:
            stored = await self._location.latest()
            if stored is not None and (self._ttl is None or stored.is_fresh(now, self._ttl)):
                return to_grid(stored.lat, stored.lon), self._label(stored, now)
        if self._grid != (0, 0):
            return self._grid, self._settings.place
        return None

    def _label(self, stored: StoredLocation, now: datetime) -> str:
        sharing = stored.live_until is not None and stored.live_until > now
        return LIVE_PLACE if sharing or stored.is_fresh(now, self._recent) else STALE_PLACE

    async def forecast(self, now: datetime, days_ahead: int = 0) -> DayForecast:
        if not self.enabled:
            raise WeatherUnavailable(DISABLED_MESSAGE)
        where = await self.where(now)
        if where is None:
            raise WeatherUnavailable(NO_LOCATION_MESSAGE)
        (nx, ny), place = where
        base_date, base_hour = base_time(now)
        params = {
            "serviceKey": self._settings.api_key,
            "pageNo": 1,
            "numOfRows": MAX_ROWS,
            "dataType": "JSON",
            "base_date": base_date,
            "base_time": base_hour,
            "nx": nx,
            "ny": ny,
        }
        try:
            response = await self._client.get(self._endpoint, params=params)
        except httpx.HTTPError as exc:
            # 예외 메시지에 인증키가 들어간 주소가 섞일 수 있어 종류만 기록한다
            logger.warning("기상청 요청 실패: %s", type(exc).__name__)
            raise WeatherUnavailable("기상청에 연결하지 못했습니다.") from None
        if response.status_code != 200:
            raise WeatherUnavailable(f"기상청 응답이 정상이 아닙니다 (HTTP {response.status_code}).")
        try:
            payload = response.json()
        except ValueError:
            raise WeatherUnavailable("기상청 응답을 읽지 못했습니다.") from None
        day = (to_kst(now) + timedelta(days=days_ahead)).date()
        forecast = build_forecast(read_items(payload), day, place)
        return replace(
            forecast, feels_warmer=self._settings.feels_warmer, umbrella_chance=self._settings.umbrella_chance
        )
