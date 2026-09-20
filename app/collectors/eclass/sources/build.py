"""정해 둔 수집 범위를 실제 소스 목록으로 바꾼다.

탐색기가 만든 카탈로그(어디를 어떻게 여는지)와 범위 결정(무엇을 어떻게 다룰지)을 합친다.
설정 파일에 화면 이름을 하나씩 적을 필요가 없다 (docs/tasks.md T-07).
"""

import logging
import re
from datetime import timedelta
from pathlib import Path

from app.collectors.eclass.scope import Screen, ScopeEntry, load_catalog
from app.collectors.eclass.sources.base import EclassSource
from app.collectors.eclass.sources.board import BoardSource
from app.collectors.eclass.sources.syllabus import SyllabusSource
from app.collectors.eclass.sources.todo import TodoSource
from app.collectors.eclass.urgent import DEFAULT_URGENT_WORDS
from app.core.config import Level
from app.storage.eclass import EclassRepository

logger = logging.getLogger(__name__)

# 수준마다 얼마 만에 한 번 볼지. 알릴 화면은 매번, 나머지는 띄엄띄엄 본다.
INTERVALS = {
    Level.NOTIFY: timedelta(0),
    Level.BRIEF: timedelta(hours=6),
    Level.STORE: timedelta(hours=12),
}
PRIORITY = {Level.NOTIFY: 0, Level.BRIEF: 1, Level.STORE: 2}

# 할 일 화면은 TodoSource가 맡는다. 마감은 그쪽이 한곳에서 본다.
TODO_MARK = "todo_list"
# 과목방 안에서만 뜻이 있는 화면
COURSE_PREFIX = "/ilos/st/course/"

# 게시판이 아니라 따로 읽어야 하는 화면. 경로에 이 조각이 있으면 전용 소스를 쓴다.
SPECIAL = {"plan_form": SyllabusSource}

# 메뉴 글자를 못 주운 화면의 이름. 알림 문구에 그대로 나오므로 사람이 읽을 말로 둔다.
FALLBACK_LABELS = {
    "message_received_list_pop": "쪽지",
    "message_sent_list_pop": "보낸 쪽지",
    "mp_notification_list": "알림함",
    "main_main_ctl_notice_list": "학교 공지",
    "main_course_ing_list": "수강 과목",
    "main_pop_academic_timetable": "시간표",
}


def source_key(path: str) -> str:
    """경로에서 소스 이름을 만든다.

    저장된 항목의 출처가 되므로 한번 정하면 바뀌지 않아야 하고, 소스끼리 겹치면 안 된다.
    공지는 전체 공지와 과목 공지가 따로 있어 마지막 한 마디만으로는 구별되지 않는다.
    """
    parts = [part for part in path.split("/") if part]
    tail = "_".join(parts[-2:]) if parts else "screen"
    tail = re.sub(r"\.acl$", "", tail)
    tail = re.sub(r"_(form|pop)$", "", tail)
    return re.sub(r"[^\w]", "_", tail) or "screen"


def source_label(name: str, key: str) -> str:
    """사용자에게 보일 이름. 메뉴 글자에 읽지 않은 글 수가 붙어 오는 경우가 있다."""
    cleaned = re.sub(r"\s*\d+$", "", (name or "").strip())
    return cleaned or FALLBACK_LABELS.get(key, key)


def build_sources(
    catalog_path: Path,
    scope: dict[str, ScopeEntry],
    items: EclassRepository | None = None,
    urgent_words: tuple[str, ...] = DEFAULT_URGENT_WORDS,
) -> list[EclassSource]:
    """켤 소스 목록. 할 일 화면은 언제나 첫 소스다."""
    sources: list[EclassSource] = [TodoSource()]
    screens = {screen.path: screen for screen in load_catalog(catalog_path)}
    # 껍데기가 불러오는 내용 주소는 그 자체로 소스가 아니다 (같은 화면을 두 번 읽게 된다)
    inner = {
        screen.data_path
        for screen in screens.values()
        if screen.data_path and screen.data_path != screen.path
    }

    for path, entry in sorted(scope.items()):
        screen = screens.get(path)
        if entry.level is Level.OFF or screen is None or path in inner:
            continue
        if TODO_MARK in path:
            continue
        special = next((cls for mark, cls in SPECIAL.items() if mark in path), None)
        sources.append(
            special(path=path, level=entry.level)
            if special
            else _board(screen, entry, items, urgent_words)
        )

    logger.info("eClass 소스 %d개 (할 일 포함)", len(sources))
    return sources


def _board(
    screen: Screen,
    entry: ScopeEntry,
    items: EclassRepository | None,
    urgent_words: tuple[str, ...] = DEFAULT_URGENT_WORDS,
) -> BoardSource:
    key = source_key(screen.path)
    return BoardSource(
        key=key,
        label=source_label(entry.name, key),
        path=screen.path,
        data_path=screen.data_path,
        level=entry.level,
        per_course=entry.per_course or screen.path.startswith(COURSE_PREFIX),
        interval=INTERVALS[entry.level],
        priority=PRIORITY[entry.level],
        items=items,
        urgent_words=urgent_words,
    )
