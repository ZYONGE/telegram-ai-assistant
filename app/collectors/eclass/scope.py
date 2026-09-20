"""eClass 수집 범위: 어떤 화면을 어떻게 다룰지 정한다.

탐색기가 만든 카탈로그(`private/eclass_catalog.json`)에는 볼 수 있는 화면이 전부 들어 있다.
FAQ 74줄, 설문 20줄 같은 것까지 다 알리면 하루 알림 상한을 오전에 다 쓴다.
그래서 화면마다 처리 수준을 정해 둔다.

정하는 순서
1. **코드 규칙**으로 뻔한 것을 먼저 정한다. 경로는 사이트 구조라 이름보다 안정적이므로 경로를 먼저 본다.
2. 규칙에 안 걸리는 화면만 가벼운 모델에게 묻는다. 모델이 없거나 실패하면 설정의 기본값을 쓴다.
   **모델 없이 규칙만으로도 끝까지 돌아간다.**
3. 사용자가 대화로 정한 것은 무엇보다 앞선다. 다시 정할 때도 덮어쓰지 않는다.

메뉴 이름은 외부에서 온 글자다. 규칙에 쓰기만 하고 지시로 해석하지 않는다 (절대 규칙 8).
결과는 학교를 특정하므로 `private/` 안에만 둔다 (절대 규칙 12).
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from app.core.clock import KST
from app.core.config import Level, ScopePolicy

logger = logging.getLogger(__name__)

# 글 하나를 여는 화면. 목록이 아니라 그 안의 글이라 수집 대상이 아니다.
VIEW_MARK = "_view"

LABELS = {
    Level.NOTIFY: "알림",
    Level.BRIEF: "브리핑",
    Level.STORE: "저장만",
    Level.OFF: "끔",
}


class Decided(StrEnum):
    """누가 정했는지. 사용자가 정한 것은 다시 정할 때 건드리지 않는다."""

    RULE = "rule"
    MODEL = "model"
    USER = "user"


@dataclass(frozen=True, slots=True)
class Screen:
    """카탈로그의 화면 한 줄. 탐색기가 판정해 둔 것만 쓴다."""

    path: str
    name: str = ""
    listing: bool = False
    has_date: bool = False
    has_due: bool = False
    per_course: bool = False
    # 껍데기가 따로 불러오는 내용 주소. 빈 값이면 화면을 열면 내용까지 온다.
    data_path: str = ""


@dataclass(frozen=True, slots=True)
class ScopeEntry:
    path: str
    name: str
    level: Level
    by: Decided = Decided.RULE
    per_course: bool = False


class ScopeClassifier(Protocol):
    """규칙에 안 걸린 화면을 분류한다. 없어도 수집은 돌아간다."""

    async def classify_screens(self, screens: list[Screen]) -> dict[str, Level]: ...


def _has(text: str, words: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(word.lower() in lowered for word in words)


def classify(screen: Screen, policy: ScopePolicy) -> Level | None:
    """코드 규칙으로 정한다. 정할 수 없으면 None."""
    path, name = screen.path, screen.name

    # 글 하나를 여는 화면은 어떤 경우에도 수집원이 아니다. 목록에서 따라 들어가는 곳이다.
    # ("notice_view_form"처럼 주제 낱말이 들어 있어도 마찬가지다.)
    if VIEW_MARK in path:
        return Level.OFF

    # 경로부터 본다. 사이트 구조라 메뉴 이름보다 덜 흔들린다.
    # 끌 것을 먼저 가린다. 알림은 적을수록 좋고, 켜는 쪽이 되돌리기 쉽다.
    if _has(path, policy.off_paths):
        return Level.OFF
    if _has(path, policy.notify_paths):
        return Level.NOTIFY
    if _has(path, policy.store_paths):
        return Level.STORE

    # 메뉴 이름은 외부에서 온 글자다. 규칙에 넣기만 한다.
    if _has(name, policy.off_words):
        return Level.OFF
    if _has(name, policy.notify_words):
        return Level.NOTIFY
    if _has(name, policy.store_words):
        return Level.STORE

    # 목록이 아니면 읽어 올 줄이 없다. 화면에 "마감"이라는 글자가 있어도 마찬가지다
    # (메인 화면이 그렇다).
    if not screen.listing:
        return Level.OFF
    # 마감이 있는 목록은 알릴 값어치가 있다
    if screen.has_due:
        return Level.NOTIFY
    if screen.has_date:
        return Level.BRIEF
    return None


async def decide(
    screens: list[Screen],
    policy: ScopePolicy,
    existing: dict[str, ScopeEntry] | None = None,
    classifier: ScopeClassifier | None = None,
) -> dict[str, ScopeEntry]:
    """화면마다 수준을 정한다. 사용자가 정한 것은 그대로 두고, 새 화면만 다시 정한다."""
    existing = existing or {}
    decided: dict[str, ScopeEntry] = {}
    unknown: list[Screen] = []

    for screen in screens:
        kept = existing.get(screen.path)
        if kept is not None and kept.by is Decided.USER:
            decided[screen.path] = kept
            continue
        level = classify(screen, policy)
        if level is None:
            unknown.append(screen)
            continue
        decided[screen.path] = ScopeEntry(
            path=screen.path, name=screen.name, level=level, by=Decided.RULE, per_course=screen.per_course
        )

    guessed: dict[str, Level] = {}
    if unknown and classifier is not None:
        try:
            guessed = await classifier.classify_screens(unknown)
        except Exception:
            logger.exception("수집 범위 분류 실패, 기본값으로 둡니다")
    for screen in unknown:
        level = guessed.get(screen.path)
        decided[screen.path] = ScopeEntry(
            path=screen.path,
            name=screen.name,
            level=level or policy.unknown,
            by=Decided.MODEL if level else Decided.RULE,
            per_course=screen.per_course,
        )

    # 사용자가 정해 둔 화면은 이번 카탈로그에 없어도 남긴다 (과목방에 못 들어간 날이 있다)
    for path, entry in existing.items():
        if entry.by is Decided.USER and path not in decided:
            decided[path] = entry
    return decided


async def ensure_scope(
    catalog_path: Path,
    policy: ScopePolicy,
    store: "ScopeStore",
    classifier: ScopeClassifier | None = None,
) -> dict[str, ScopeEntry]:
    """비서를 켤 때 한 번 부른다. 새 화면이 생겼을 때만 다시 정한다.

    카탈로그가 없으면 아무것도 하지 않는다 (탐색기를 아직 안 돌린 것이다).
    """
    screens = load_catalog(catalog_path)
    if not screens:
        return store.read()
    existing = store.read()
    fresh = [screen for screen in screens if screen.path not in existing]
    if existing and not fresh:
        return existing

    scope = await decide(screens, policy, existing, classifier)
    store.write(scope)
    logger.info(
        "eClass 수집 범위: 화면 %d개 중 %d개를 가져옵니다 (새 화면 %d개)",
        len(scope),
        len(collected(scope)),
        len(fresh),
    )
    return scope


def collected(scope: dict[str, ScopeEntry]) -> list[ScopeEntry]:
    """실제로 가져올 화면. 알림 수준이 높은 것부터."""
    order = {Level.NOTIFY: 0, Level.BRIEF: 1, Level.STORE: 2}
    return sorted(
        (entry for entry in scope.values() if entry.level is not Level.OFF),
        key=lambda entry: (order[entry.level], entry.path),
    )


def load_catalog(path: Path) -> list[Screen]:
    """탐색기가 만든 카탈로그를 읽는다. 열리지 않은 화면은 뺀다."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    screens = []
    for row in data.get("screens", []):
        if row.get("note"):
            continue
        screens.append(
            Screen(
                path=str(row.get("path", "")),
                name=str(row.get("name", "")),
                listing=bool(row.get("listing")),
                has_date=bool(row.get("has_date")),
                has_due=bool(row.get("has_due")),
                per_course=bool(row.get("per_course")),
                data_path=str(row.get("data_path", "")),
            )
        )
    return [screen for screen in screens if screen.path]


class ScopeStore:
    """정해 둔 수준을 파일 하나에 담는다. 개인 파일이므로 private/ 안에 있다."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> dict[str, ScopeEntry]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("수집 범위 파일을 읽지 못해 처음부터 정합니다")
            return {}
        entries = {}
        for path, row in (data.get("screens") or {}).items():
            try:
                entries[path] = ScopeEntry(
                    path=path,
                    name=str(row.get("name", "")),
                    level=Level(row.get("level", Level.OFF)),
                    by=Decided(row.get("by", Decided.RULE)),
                    per_course=bool(row.get("per_course")),
                )
            except ValueError:
                logger.warning("모르는 수집 수준이 있어 건너뜁니다")
        return entries

    def write(self, scope: dict[str, ScopeEntry]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "made_at": datetime.now(KST).isoformat(timespec="seconds"),
            "screens": {
                entry.path: {
                    "name": entry.name,
                    "level": str(entry.level),
                    "by": str(entry.by),
                    "per_course": entry.per_course,
                }
                for entry in scope.values()
            },
        }
        self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def set_level(self, path: str, level: Level) -> ScopeEntry:
        """사용자가 대화로 바꾼다. 다시 정할 때 덮어쓰지 않도록 표시해 둔다."""
        scope = self.read()
        kept = scope.get(path)
        entry = ScopeEntry(
            path=path,
            name=kept.name if kept else "",
            level=level,
            by=Decided.USER,
            per_course=kept.per_course if kept else False,
        )
        scope[path] = entry
        self.write(scope)
        return entry
