"""날씨 도구. 예보 조회는 수집기가 하고, 여기서는 모델이 쓸 문장으로만 넘긴다."""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from app.collectors.weather import KmaWeather, WeatherUnavailable
from app.core.clock import utc_now
from app.core.interfaces import ToolResult
from app.tools.common import SimpleTool, optional_str, spec

DAYS = {"오늘": 0, "today": 0, "내일": 1, "tomorrow": 1, "모레": 2}
LABELS = {0: "오늘", 1: "내일", 2: "모레"}


def weather_tools(weather: KmaWeather, clock: Callable[[], datetime] = utc_now) -> list:
    async def get_weather(args: Mapping[str, Any]) -> ToolResult:
        day = (optional_str(args, "day") or "오늘").lower()
        days_ahead = DAYS.get(day, 0)
        try:
            forecast = await weather.forecast(clock(), days_ahead)
        except WeatherUnavailable as exc:
            return ToolResult(str(exc), is_error=True)
        label = LABELS[days_ahead]
        # 옷차림 기본안은 코드가 정한다. 모델은 일정·상황을 반영해 문장만 다듬는다.
        return ToolResult(f"{label} 날씨\n{forecast.render()}\n{forecast.clothing()}")

    return [
        SimpleTool(
            spec(
                "get_weather",
                "오늘·내일·모레의 아침·점심·저녁 기온, 하늘 상태, 강수확률과 옷차림 기본안을 가져온다. "
                "날씨나 옷차림을 물으면 사용한다. 결과의 옷차림 기본안은 그대로 쓰되 일정이나 상황에 맞게 문장만 다듬는다.",
                {"day": {"type": "string", "enum": ["오늘", "내일", "모레"], "description": "조회할 날 (기본값 오늘)"}},
                [],
            ),
            get_weather,
        )
    ]
