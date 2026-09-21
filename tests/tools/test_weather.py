import httpx
import pytest

from app.collectors.weather import (
    DISABLED_MESSAGE,
    LIVE_PLACE,
    NO_LOCATION_MESSAGE,
    STALE_PLACE,
    KmaWeather,
    WeatherUnavailable,
    base_time,
    build_forecast,
    read_items,
    to_grid,
)
from app.core.config import WeatherSettings
from app.core.interfaces import BriefingKind
from app.scheduler.briefing import WeatherBriefing
from app.storage.location import StoredLocation
from app.tools.weather import weather_tools
from tests.conftest import kst

KEY = "test-service-key-1234"


def item(category: str, value: str, hour: int, day: str = "20260918") -> dict:
    return {"category": category, "fcstValue": value, "fcstDate": day, "fcstTime": f"{hour:02d}00"}


def payload(items: list[dict], code: str = "00", message: str = "NORMAL_SERVICE") -> dict:
    return {
        "response": {
            "header": {"resultCode": code, "resultMsg": message},
            "body": {"dataType": "JSON", "items": {"item": items}},
        }
    }


# 아침 18도 흐림, 점심 24도 맑음, 저녁 20도 비 (강수 70%)
SAMPLE = [
    item("TMN", "16.0", 6),
    item("TMX", "25.0", 15),
    item("TMP", "18", 7), item("SKY", "4", 7), item("PTY", "0", 7), item("POP", "10", 7),
    item("TMP", "24", 12), item("SKY", "1", 12), item("PTY", "0", 12), item("POP", "20", 12),
    item("TMP", "20", 18), item("SKY", "4", 18), item("PTY", "1", 18), item("POP", "70", 18),
    item("TMP", "9", 7, day="20260919"),
]


def weather_with(handler, **options) -> tuple[KmaWeather, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = WeatherSettings(api_key=KEY, nx=60, ny=127, **options)
    return KmaWeather(settings, client), client


def ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=payload(SAMPLE))


def test_to_grid_matches_known_point():
    # 서울시청 좌표는 기상청 격자 (60, 127)이다
    assert to_grid(37.5665, 126.9780) == (60, 127)


def test_grid_is_taken_from_lat_lon_when_missing():
    weather = KmaWeather(WeatherSettings(api_key=KEY, lat=37.5665, lon=126.9780), httpx.AsyncClient())
    assert weather.enabled


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (kst(9, 18, 7), ("20260918", "0500")),
        (kst(9, 18, 8, 5), ("20260918", "0500")),
        (kst(9, 18, 8, 30), ("20260918", "0800")),
        (kst(9, 18, 23, 59), ("20260918", "2300")),
        (kst(9, 18, 1, 30), ("20260917", "2300")),
    ],
)
def test_base_time_uses_last_published_forecast(now, expected):
    assert base_time(now) == expected


def test_build_forecast_reads_three_slots():
    forecast = build_forecast(SAMPLE, kst(9, 18, 7).date(), place="우리 동네")
    assert [slot.name for slot in forecast.slots] == ["아침", "점심", "저녁"]
    assert (forecast.low, forecast.high) == (16.0, 25.0)
    assert forecast.render() == (
        "우리 동네 아침 18도 흐림 · 점심 24도 맑음 · 저녁 20도 비 강수 70% (최저 16도 / 최고 25도)"
    )


def test_clothing_uses_code_table_and_adds_umbrella_and_gap():
    advice = build_forecast(SAMPLE, kst(9, 18, 7).date()).clothing()
    assert advice.startswith("옷차림: 얇은 니트, 맨투맨, 가디건")
    assert "일교차가 9도" in advice
    assert "저녁에 비 소식이 있어 우산을 챙기세요." in advice


@pytest.mark.parametrize(
    ("temp", "expected"),
    [("30", "민소매"), ("24", "반팔"), ("21", "긴팔"), ("18", "얇은 니트"), ("13", "자켓"), ("10", "트렌치"), ("6", "코트"), ("-3", "패딩")],
)
def test_clothing_table_covers_every_range(temp, expected):
    items = [item("TMP", temp, 7), item("TMP", temp, 12), item("TMP", temp, 18)]
    assert expected in build_forecast(items, kst(9, 18, 7).date()).clothing()


def test_missing_day_is_reported():
    with pytest.raises(WeatherUnavailable, match="예보가 아직 없습니다"):
        build_forecast(SAMPLE, kst(9, 25, 7).date())


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (payload([], code="30", message="SERVICE KEY IS NOT REGISTERED ERROR"), "처리하지 못했습니다"),
        ({"response": {"header": {"resultCode": "00"}}}, "읽지 못했습니다"),
        ({"cmmMsgHeader": "error"}, "읽지 못했습니다"),
    ],
)
def test_bad_payloads_are_reported(data, message):
    with pytest.raises(WeatherUnavailable, match=message):
        read_items(data)


async def test_forecast_requests_expected_parameters():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json=payload(SAMPLE))

    weather, client = weather_with(handler, place="우리 동네")
    async with client:
        forecast = await weather.forecast(kst(9, 18, 7))
    assert seen["base_date"] == "20260918" and seen["base_time"] == "0500"
    assert seen["nx"] == "60" and seen["ny"] == "127" and seen["dataType"] == "JSON"
    assert forecast.place == "우리 동네"


async def test_tomorrow_uses_next_day():
    weather, client = weather_with(ok_handler)
    async with client:
        with pytest.raises(WeatherUnavailable):
            # 표본에는 내일 아침 기온만 있어 점심·저녁이 없다 → 아침만 채워진다
            await weather.forecast(kst(9, 18, 7), days_ahead=2)
        forecast = await weather.forecast(kst(9, 18, 7), days_ahead=1)
    assert forecast.slots[0].temp == 9.0 and forecast.slots[1].temp is None


async def test_disabled_without_key_or_grid():
    weather = KmaWeather(WeatherSettings(), httpx.AsyncClient())
    assert weather.enabled is False
    with pytest.raises(WeatherUnavailable, match="날씨 설정이 아직 없습니다"):
        await weather.forecast(kst(9, 18, 7))


@pytest.mark.parametrize(
    "response",
    [httpx.Response(500, text="server error"), httpx.Response(200, text="<xml>not json</xml>")],
)
async def test_server_problems_do_not_leak_the_key(response):
    weather, client = weather_with(lambda request: response)
    async with client:
        with pytest.raises(WeatherUnavailable) as error:
            await weather.forecast(kst(9, 18, 7))
    assert KEY not in str(error.value)


async def test_connection_error_does_not_leak_the_key(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("failed", request=request)

    weather, client = weather_with(handler)
    async with client:
        with pytest.raises(WeatherUnavailable, match="연결하지 못했습니다") as error:
            await weather.forecast(kst(9, 18, 7))
    assert KEY not in str(error.value) and KEY not in caplog.text


async def test_tool_returns_forecast_and_clothing():
    weather, client = weather_with(ok_handler)
    tool = weather_tools(weather, clock=lambda: kst(9, 18, 7))[0]
    async with client:
        result = await tool.run({})
        tomorrow = await tool.run({"day": "내일"})
    assert result.is_error is False
    assert result.content.startswith("오늘 날씨\n아침 18도 흐림")
    assert "옷차림:" in result.content
    assert tomorrow.content.startswith("내일 날씨")


async def test_tool_reports_missing_setup_as_error():
    tool = weather_tools(KmaWeather(WeatherSettings(), httpx.AsyncClient()))[0]
    result = await tool.run({})
    assert result.is_error is True and result.content == DISABLED_MESSAGE


async def test_briefing_items_morning_and_evening():
    weather, client = weather_with(ok_handler, place="우리 동네")
    provider = WeatherBriefing(weather)
    async with client:
        morning = await provider.briefing_items(BriefingKind.MORNING, kst(9, 18, 7))
        evening = await provider.briefing_items(BriefingKind.EVENING, kst(9, 18, 22))
    assert [i.section for i in morning] == ["오늘 날씨", "오늘 날씨"]
    assert morning[0].priority > morning[1].priority
    assert morning[1].text.startswith("옷차림:")
    assert [i.section for i in evening] == ["내일 날씨"]


async def test_briefing_skips_when_weather_is_off_or_failing():
    off = WeatherBriefing(KmaWeather(WeatherSettings(), httpx.AsyncClient()))
    assert await off.briefing_items(BriefingKind.MORNING, kst(9, 18, 7)) == []

    weather, client = weather_with(lambda request: httpx.Response(503, text="down"))
    async with client:
        assert await WeatherBriefing(weather).briefing_items(BriefingKind.MORNING, kst(9, 18, 7)) == []


# --- 실시간 위치 따라가기 ---


class FakeLocations:
    def __init__(self, stored: StoredLocation | None = None) -> None:
        self.stored = stored

    async def latest(self) -> StoredLocation | None:
        return self.stored


def located_weather(stored, handler=ok_handler, **options):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = WeatherSettings(api_key=KEY, **options)
    return KmaWeather(settings, client, location=FakeLocations(stored)), client


async def test_recent_location_is_used_instead_of_configured_place():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json=payload(SAMPLE))

    # 부산 좌표를 보냈으면 서울 고정 좌표 대신 그 위치를 본다
    stored = StoredLocation(35.1796, 129.0756, kst(9, 18, 6))
    weather, client = located_weather(stored, handler, nx=60, ny=127, place="설정 동네")
    async with client:
        forecast = await weather.forecast(kst(9, 18, 7))

    assert (seen["nx"], seen["ny"]) != ("60", "127")
    assert (int(seen["nx"]), int(seen["ny"])) == to_grid(35.1796, 129.0756)
    assert forecast.place == LIVE_PLACE


async def test_old_location_is_kept_by_default_but_labelled():
    # 기본값(location_ttl_hours = 0)은 만료 없이 마지막 위치를 계속 쓴다
    stored = StoredLocation(35.1796, 129.0756, kst(9, 16, 7))  # 이틀 전
    weather, client = located_weather(stored, nx=60, ny=127, place="설정 동네")
    async with client:
        forecast = await weather.forecast(kst(9, 18, 7))
    assert forecast.place == STALE_PLACE


async def test_live_sharing_is_always_current():
    stored = StoredLocation(35.1796, 129.0756, kst(9, 18, 0), live_until=kst(9, 18, 8))
    weather, client = located_weather(stored)
    async with client:
        forecast = await weather.forecast(kst(9, 18, 7))
    assert forecast.place == LIVE_PLACE


async def test_expiry_can_be_turned_on():
    stored = StoredLocation(35.1796, 129.0756, kst(9, 16, 7))
    weather, client = located_weather(stored, nx=60, ny=127, place="설정 동네", location_ttl_hours=24)
    async with client:
        forecast = await weather.forecast(kst(9, 18, 7))
    assert forecast.place == "설정 동네"


async def test_location_can_be_turned_off():
    stored = StoredLocation(35.1796, 129.0756, kst(9, 18, 6))
    weather, client = located_weather(
        stored, nx=60, ny=127, place="설정 동네", follow_telegram_location=False
    )
    async with client:
        forecast = await weather.forecast(kst(9, 18, 7))
    assert forecast.place == "설정 동네"


async def test_without_any_location_the_user_is_asked_to_send_one():
    weather, client = located_weather(None)
    assert weather.enabled is True
    async with client:
        with pytest.raises(WeatherUnavailable, match="위치를 보내 주세요"):
            await weather.forecast(kst(9, 18, 7))


async def test_tool_asks_for_location_when_unknown():
    weather, client = located_weather(None)
    async with client:
        result = await weather_tools(weather, clock=lambda: kst(9, 18, 7))[0].run({})
    assert result.is_error is True and result.content == NO_LOCATION_MESSAGE


def test_clothing_follows_how_warm_the_user_runs():
    """더위를 타는 편이면 같은 기온에서도 한 단계 가볍게 권한다 (설정 feels_warmer)."""
    from dataclasses import replace

    from app.collectors.weather import SlotForecast

    day = build_forecast(SAMPLE, kst(9, 18, 7).date())
    cool = replace(day, slots=(SlotForecast("아침", 16.0, "맑음", "", 0),), low=16.0, high=16.0)
    assert "자켓이나 야상" in cool.clothing()
    assert "얇은 니트" in replace(cool, feels_warmer=2.0).clothing()


def test_a_lower_umbrella_line_catches_light_rain():
    from dataclasses import replace

    from app.collectors.weather import SlotForecast

    day = build_forecast(SAMPLE, kst(9, 18, 7).date())
    drizzle = replace(day, slots=(SlotForecast("점심", 20.0, "흐림", "", 40),))
    assert "우산" not in drizzle.clothing()
    assert "우산" in replace(drizzle, umbrella_chance=30).clothing()
