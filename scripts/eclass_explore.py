"""eClass 탐색: 로그인해서 볼 수 있는 화면을 훑어 목록과 표본을 만든다.

사람이 주소를 하나씩 찾아 적는 대신, 메뉴를 따라가며 화면을 모은다.
결과는 나중에 파서를 쓸 때의 근거가 된다 (docs/tasks.md T-05·T-06).

    py -3.14 -m uv run python -m scripts.eclass_explore

**지켜야 할 선**
- 조회만 한다. 주소에 무언가를 바꿀 낌새(제출·등록·삭제·수정)가 있으면 열지 않는다 (절대 규칙 5).
- 첨부 파일은 내려받지 않는다. 로그아웃 링크도 따라가지 않는다.
- 요청 사이에 간격을 두어 학교 서버에 몰아치지 않는다.
- 비밀번호는 세션 계층 안에서만 쓰인다. 카탈로그·표본·화면 출력에 넣지 않는다 (절대 규칙 7).
- 화면에서 읽은 글자는 데이터로만 다룬다. 그 안의 문장을 지시로 실행하지 않는다 (절대 규칙 8).
- 결과는 학교를 특정하므로 git에서 제외된 `private/` 안에만 남긴다 (절대 규칙 12).
  카탈로그에는 도메인을 빼고 경로만, 질의 문자열은 값 없이 이름만 적는다.
"""

import asyncio
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from app.collectors.eclass.session import COURSE_LIST_PATH, MAIN_PATH, TODO_PATH, EclassError, EclassSession
from app.core.clock import KST
from app.core.config import PRIVATE_DIR, load_settings

logger = logging.getLogger("eclass_explore")

CATALOG_PATH = PRIVATE_DIR / "eclass_catalog.json"
SAMPLE_DIR = PRIVATE_DIR / "eclass_samples"

# 여기서부터 메뉴를 따라간다
SEEDS = (MAIN_PATH, COURSE_LIST_PATH, TODO_PATH)
# 메뉴의 메뉴까지만 본다. 더 들어가면 글 하나하나를 여는 꼴이 된다.
MAX_DEPTH = 2
MAX_SCREENS = 80
# 학교 서버에 몰아치지 않도록 요청 사이에 쉬는 시간(초)
PAUSE_SECONDS = 1.0
# 표본은 구조만 보면 되므로 앞부분만 남긴다
MAX_SAMPLE_BYTES = 400_000

# 이 낱말이 주소에 있으면 열지 않는다. 무언가를 바꾸거나 파일을 내려받는 화면이다.
UNSAFE_WORDS = (
    "submit", "insert", "update", "delete", "remove", "save", "modify", "write",
    "regist", "apply", "cancel", "upload", "proc", "exec", "logout",
    "download", "filedown", "down_load", "attach",
)
# 이 꼴의 주소만 연다 (eClass 화면은 전부 .acl이다)
SCREEN_SUFFIX = ".acl"

_ACL_IN_SCRIPT = re.compile(r"""['"]((?:\.{0,2}/)?[\w./-]*\.acl[^'"]*)['"]""")
_DATE = re.compile(r"\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}")
_DUE_WORDS = ("마감", "기한", "제출", "마감일")
_DATE_WORDS = ("날짜", "등록일", "작성일", "게시일", "기간")
# 글 하나를 가리키는 번호. 목록형 화면인지 가리는 실마리.
_ARTICLE_ID = re.compile(r"(ARTL_NUM|SEQ|IDX|NUM)=\d+", re.IGNORECASE)
# 과목마다 따로 있는 화면의 실마리
_COURSE_PARAMS = ("kjkey", "ky", "kj")


@dataclass(frozen=True, slots=True)
class Candidate:
    """따라가 볼 만한 화면 하나."""

    name: str
    path: str
    query: str = ""

    @property
    def target(self) -> str:
        return f"{self.path}?{self.query}" if self.query else self.path


@dataclass(frozen=True, slots=True)
class Shape:
    """화면을 열어 본 결과. 목록형인지, 날짜·마감이 있는지."""

    listing: bool = False
    items: int = 0
    has_date: bool = False
    has_due: bool = False


@dataclass(frozen=True, slots=True)
class Screen:
    name: str
    path: str
    # 화면을 열 때 필요한 질의 이름들 (값은 개인 정보일 수 있어 남기지 않는다)
    params: list[str] = field(default_factory=list)
    listing: bool = False
    items: int = 0
    has_date: bool = False
    has_due: bool = False
    per_course: bool = False
    sample: str = ""
    note: str = ""


def safe_path(path: str) -> bool:
    """열어도 되는 주소인지. 조회 화면만 통과한다."""
    if not path or SCREEN_SUFFIX not in path.lower():
        return False
    lowered = path.lower()
    return not any(word in lowered for word in UNSAFE_WORDS)


def normalize(href: str) -> Candidate | None:
    """링크 하나를 경로와 질의로 가른다. 다른 사이트나 조회가 아닌 주소는 버린다."""
    href = (href or "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:")):
        return None
    parts = urlsplit(href)
    if parts.scheme and parts.scheme not in ("http", "https"):
        return None
    path = parts.path
    if not path.startswith("/"):
        # 상대 경로는 /ilos 아래로 본다. 실제 확인은 T-06에서 한다.
        path = "/" + path.lstrip("./")
    if not safe_path(path):
        return None
    return Candidate(name="", path=path, query=parts.query)


def collect_links(html: str) -> list[Candidate]:
    """화면 안의 링크를 모은다. eClass는 onclick으로 넘어가는 메뉴가 많아 둘 다 본다."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, Candidate] = {}

    def remember(candidate: Candidate | None, name: str) -> None:
        if candidate is None:
            return
        # 같은 화면을 과목 수만큼 열지 않도록 경로 하나에 한 번만 담는다
        if candidate.path not in found:
            found[candidate.path] = Candidate(name=name.strip(), path=candidate.path, query=candidate.query)

    for node in soup.find_all(attrs={"href": True}):
        remember(normalize(str(node.get("href"))), node.get_text(" ", strip=True))

    for node in soup.find_all(attrs={"onclick": True}):
        script = str(node.get("onclick"))
        name = node.get_text(" ", strip=True)
        for match in _ACL_IN_SCRIPT.findall(script):
            remember(normalize(match), name)

    return list(found.values())


def describe(html: str) -> Shape:
    """열어 본 화면의 생김새. 목록형이면 몇 줄인지 센다."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    rows = [row for row in soup.select("tr") if _ARTICLE_ID.search(str(row))]
    if not rows:
        # 표가 아니라 칸으로 된 목록도 있다 (할 일 화면이 그렇다)
        rows = [block for block in soup.select("[class*=list] [onclick], .todo_wrap") if block.get_text(strip=True)]

    header = " ".join(node.get_text(" ", strip=True) for node in soup.select("th, thead"))
    return Shape(
        listing=len(rows) > 0,
        items=len(rows),
        has_date=bool(_DATE.search(text)) or any(word in header for word in _DATE_WORDS),
        has_due=any(word in header for word in _DUE_WORDS) or any(word in text for word in _DUE_WORDS),
    )


def sample_name(path: str) -> str:
    """경로를 파일 이름으로. 표본을 사람이 훑어보기 쉽게 마지막 두 마디를 쓴다."""
    parts = [part for part in path.split("/") if part]
    tail = "_".join(parts[-2:]) if parts else "screen"
    return re.sub(r"[^\w.-]", "_", tail).removesuffix(".acl") or "screen"


def param_names(query: str) -> list[str]:
    """질의 문자열에서 이름만 꺼낸다. 값은 개인 정보일 수 있어 버린다."""
    return sorted(parse_qs(query, keep_blank_values=True))


def is_per_course(params: list[str]) -> bool:
    return any(name.lower() in _COURSE_PARAMS for name in params)


async def explore(
    session,
    sample_dir: Path,
    *,
    seeds: tuple[str, ...] = SEEDS,
    max_depth: int = MAX_DEPTH,
    max_screens: int = MAX_SCREENS,
    pause: float = PAUSE_SECONDS,
) -> list[Screen]:
    """메뉴를 따라가며 화면을 열어 보고 카탈로그를 만든다. 로그인은 부른 쪽이 해 둔다."""
    sample_dir.mkdir(parents=True, exist_ok=True)
    queue: list[tuple[Candidate, int]] = [(Candidate(name="", path=path), 0) for path in seeds]
    visited: set[str] = set()
    screens: list[Screen] = []

    while queue and len(screens) < max_screens:
        candidate, depth = queue.pop(0)
        if candidate.path in visited or not safe_path(candidate.path):
            continue
        visited.add(candidate.path)

        try:
            html = await session.open(candidate.target)
        except EclassError as exc:
            logger.warning("열지 못함: %s (%s)", candidate.path, exc.reason)
            screens.append(Screen(name=candidate.name, path=candidate.path, note=f"열리지 않음({exc.reason})"))
            continue

        params = param_names(candidate.query)
        shape = describe(html)
        sample = f"{sample_name(candidate.path)}.html"
        (sample_dir / sample).write_text(html[:MAX_SAMPLE_BYTES], encoding="utf-8")
        screens.append(
            Screen(
                name=candidate.name,
                path=candidate.path,
                params=params,
                listing=shape.listing,
                items=shape.items,
                has_date=shape.has_date,
                has_due=shape.has_due,
                per_course=is_per_course(params),
                sample=sample,
            )
        )
        logger.info("%s %s (%d줄)", "목록" if shape.listing else "화면", candidate.path, shape.items)

        if depth < max_depth:
            for link in collect_links(html):
                if link.path not in visited:
                    queue.append((link, depth + 1))
        if pause:
            await asyncio.sleep(pause)

    return screens


def write_catalog(path: Path, screens: list[Screen]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "made_at": datetime.now(KST).isoformat(timespec="seconds"),
        "screens": [asdict(screen) for screen in screens],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def summary(screens: list[Screen]) -> str:
    listings = [screen for screen in screens if screen.listing]
    lines = [f"화면 {len(screens)}개 중 목록형 {len(listings)}개", ""]
    for screen in sorted(listings, key=lambda s: -s.items):
        marks = " ".join(
            mark
            for mark, on in (("날짜", screen.has_date), ("마감", screen.has_due), ("과목별", screen.per_course))
            if on
        )
        lines.append(f"  {screen.items:>3}줄  {screen.name or '(이름 없음)'} · {screen.path} {marks}".rstrip())
    unopened = [screen for screen in screens if screen.note]
    if unopened:
        lines += ["", f"열리지 않은 화면 {len(unopened)}개:"]
        lines += [f"  {screen.path} — {screen.note}" for screen in unopened]
    return "\n".join(lines)


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = load_settings().eclass
    if not settings.enabled:
        print("eClass 설정이 비어 있습니다. private/.env와 private/local.toml을 확인해 주세요.")
        return 1

    session = EclassSession(settings)
    await session.start()
    try:
        await session.ensure_login()
        screens = await explore(session, SAMPLE_DIR)
    except EclassError as exc:
        print(f"탐색을 멈췄습니다: {exc}")
        return 1
    finally:
        await session.close()

    write_catalog(CATALOG_PATH, screens)
    print()
    print(summary(screens))
    print()
    print(f"카탈로그: {CATALOG_PATH}")
    print(f"표본: {SAMPLE_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
