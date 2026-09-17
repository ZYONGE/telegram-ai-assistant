"""도구 공통: 입력 검사와 시각 해석."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any

from app.core.clock import KST
from app.core.interfaces import Confirmation, ToolSpec

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 도구 설명에 공통으로 붙이는 시각 형식 안내
TIME_FORMAT_HINT = "ISO 8601 형식. 시간대가 없으면 Asia/Seoul로 해석한다. 예: 2026-09-20T18:00"


class ToolInputError(ValueError):
    """모델이 잘못된 입력을 넘겼을 때. 메시지는 모델이 고칠 수 있게 구체적으로 쓴다."""


@dataclass(frozen=True, slots=True)
class SimpleTool:
    """spec과 실행 함수만으로 만드는 도구."""

    spec: ToolSpec
    handler: Any

    async def run(self, args: Mapping[str, Any]):
        return await self.handler(args)


def spec(name: str, description: str, properties: dict[str, Any], required: list[str], *,
         confirmation: Confirmation = Confirmation.IMMEDIATE) -> ToolSpec:
    schema = {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
    return ToolSpec(name, description, schema, confirmation)


def require_str(args: Mapping[str, Any], key: str, *, max_len: int = 500) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"'{key}' 값이 필요합니다.")
    value = value.strip()
    if len(value) > max_len:
        raise ToolInputError(f"'{key}'는 {max_len}자 이하로 적어 주세요.")
    return value


def optional_str(args: Mapping[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolInputError(f"'{key}'는 문자열이어야 합니다.")
    return value.strip()


def require_int(args: Mapping[str, Any], key: str) -> int:
    value = args.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolInputError(f"'{key}'는 정수여야 합니다.")
    return value


def parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ToolInputError(f"시각 형식이 잘못되었습니다: {value!r}. {TIME_FORMAT_HINT}") from exc
    return parsed if parsed.utcoffset() is not None else parsed.replace(tzinfo=KST)


def parse_due(value: str) -> datetime:
    """마감: 날짜만 주면 그날 23:59(Asia/Seoul)로 본다."""
    value = value.strip()
    if _DATE_ONLY.match(value):
        try:
            day = datetime.fromisoformat(value).date()
        except ValueError as exc:
            raise ToolInputError(f"날짜 형식이 잘못되었습니다: {value!r}") from exc
        return datetime.combine(day, time(23, 59), tzinfo=KST)
    return parse_datetime(value)
