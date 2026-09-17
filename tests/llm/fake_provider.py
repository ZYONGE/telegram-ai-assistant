"""제공사 교체 테스트용 가짜 어댑터."""

from dataclasses import dataclass

from app.llm import LLM


@dataclass
class FakeModel:
    name: str
    options: dict


async def create(settings) -> LLM:
    async def close() -> None:
        return None

    return LLM(
        chat=FakeModel(settings.chat_model, settings.options),
        light=FakeModel(settings.light_model, settings.options),
        close=close,
    )
