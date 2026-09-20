"""eClass 수집 소스. 화면 하나에 소스 하나.

새 화면을 붙일 때는 이 폴더에 파일 하나를 만들고 `default_sources()`에 더한다.
수집기 본체(`collector.py`)는 고치지 않는다.
"""

from app.collectors.eclass.sources.base import EclassSource, SourceResult
from app.collectors.eclass.sources.board import BoardSource
from app.collectors.eclass.sources.build import build_sources
from app.collectors.eclass.sources.todo import TodoSource

__all__ = [
    "BoardSource",
    "EclassSource",
    "SourceResult",
    "TodoSource",
    "build_sources",
    "default_sources",
]


def default_sources() -> list[EclassSource]:
    """켜 둘 소스. 위에서부터 순서대로 돈다.

    공지·쪽지·게시판은 탐색 결과에서 만들어진다 (`build_sources`). 여기는 그것이 없을 때의 기본값이다.
    """
    return [TodoSource()]
