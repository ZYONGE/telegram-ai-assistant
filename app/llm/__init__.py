"""모델 호출 모듈.

설정 파일의 [llm] provider로 제공사를 고른다. 새 제공사를 붙이려면
`async def create(settings: LLMSettings) -> LLM`을 가진 어댑터 모듈을 만들고 PROVIDERS에 등록한다.
어댑터는 선택된 것만 불러오므로, 쓰지 않는 제공사의 SDK는 설치하지 않아도 된다.
"""

import importlib

from app.core.config import ConfigError, LLMSettings
from app.llm.base import (
    LLM,
    ChatModel,
    Finish,
    LLMError,
    ModelTurn,
    SearchResult,
    ToolCall,
    TransientLLMError,
    Turn,
    WebSearch,
)

PROVIDERS: dict[str, str] = {
    "gemini": "app.llm.gemini",
}

__all__ = [
    "LLM",
    "PROVIDERS",
    "ChatModel",
    "Finish",
    "LLMError",
    "ModelTurn",
    "SearchResult",
    "ToolCall",
    "TransientLLMError",
    "Turn",
    "WebSearch",
    "create_llm",
]


async def create_llm(settings: LLMSettings) -> LLM:
    module_path = PROVIDERS.get(settings.provider)
    if module_path is None:
        raise ConfigError(
            f"지원하지 않는 모델 제공사입니다: '{settings.provider}' (사용 가능: {', '.join(sorted(PROVIDERS))})"
        )
    provider = importlib.import_module(module_path)
    return await provider.create(settings)
