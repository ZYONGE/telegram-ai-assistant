import pytest

import app.llm as llm_module
from app.core.config import ConfigError, LLMSettings
from app.llm import LLM, create_llm


async def test_unknown_provider_is_config_error():
    with pytest.raises(ConfigError, match="지원하지 않는 모델 제공사.*gemini"):
        await create_llm(LLMSettings(provider="unknown"))


async def test_provider_is_selected_by_settings(monkeypatch):
    monkeypatch.setitem(llm_module.PROVIDERS, "fake", "tests.llm.fake_provider")
    llm = await create_llm(LLMSettings(provider="fake", chat_model="c", light_model="l", options={"x": 1}))
    assert isinstance(llm, LLM)
    assert llm.chat.name == "c" and llm.light.name == "l"
    assert llm.chat.options == {"x": 1}


def test_sdk_is_used_only_inside_llm_package():
    """제공사 SDK는 app/llm 밖에서 직접 쓰지 않는다 (모델 호출을 한 모듈로 모으는 설계)."""
    from pathlib import Path

    app_dir = Path(__file__).parents[2] / "app"
    offenders = [
        str(path.relative_to(app_dir))
        for path in app_dir.rglob("*.py")
        if "llm" not in path.relative_to(app_dir).parts
        and any(sdk in path.read_text(encoding="utf-8") for sdk in ("google.genai", "from google import", "anthropic", "openai"))
    ]
    assert offenders == []
