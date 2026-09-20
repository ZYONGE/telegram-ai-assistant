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

from app.collectors.eclass.parse import parse_course_select
from app.collectors.eclass.session import (
    COURSE_LIST_PATH,
    COURSE_ROOM_PATH,
    MAIN_PATH,
    TODO_PATH,
    EclassError,
    EclassSession,
)
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
    # 내려받기는 어떤 꼴이든 막는다 (down_load·filedown·file_down 모두)
    "down", "attach",
)
# 이 꼴의 주소만 연다 (eClass 화면은 전부 .acl이다)
SCREEN_SUFFIX = ".acl"
# 인라인 스크립트에서 주운 주소는 이 꼴만 따라간다. 메뉴는 대개 스크립트 안에 있지만,
# 거기에는 읽기만 하는 주소와 무언가를 바꾸는 주소가 섞여 있다.
READING_TAILS = ("_form.acl", "_list.acl", "_view.acl")

# 과목방 안에서만 뜻이 있는 화면. 방 밖에서 열면 빈 껍데기가 온다.
COURSE_PREFIX = "/ilos/st/course/"
# 메뉴 구조는 과목마다 같다. 다만 비어 있는 과목이 있어 둘까지 본다.
MAX_COURSES = 2

_ACL_IN_SCRIPT = re.compile(r"""['"]((?:\.{0,2}/)?[\w./-]*\.acl[^'"]*)['"]""")
# 목록 화면은 껍데기(_form.acl)가 내용(같은 이름에서 _form을 뺀 주소)을 따로 불러온다.
# 할 일 화면에서 확인한 구조이고, 공지에서도 같았다 (2026-09-20).
_SHELL_SUFFIX = "_form.acl"
_DATE = re.compile(r"\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}")
_DUE_WORDS = ("마감", "기한", "제출", "마감일")
_DATE_WORDS = ("날짜", "등록일", "작성일", "게시일", "기간")
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
    # 껍데기가 따로 불러오는 내용 주소. 파서는 이쪽을 읽어야 한다.
    data_path: str = ""
    data_sample: str = ""
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


def collect_links(html: str, *, with_scripts: bool = True) -> list[Candidate]:
    """화면 안의 링크를 모은다. eClass는 onclick으로 넘어가는 메뉴가 많아 둘 다 본다.

    머리글 스크립트는 모든 화면에 똑같이 들어 있다. 그래서 스크립트 훑기는
    메뉴가 있는 첫 화면에서만 한다. 안 그러면 같은 주소를 끝없이 다시 찾는다.
    """
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

    # 메뉴를 인라인 스크립트에서 만드는 화면이 많다. 다만 거기에는 바꾸는 주소도 섞여 있어
    # 읽기만 하는 꼴(_form·_list·_view)만 따라간다.
    for node in soup.find_all("script") if with_scripts else []:
        for match in _ACL_IN_SCRIPT.findall(node.get_text()):
            candidate = normalize(match)
            if candidate is not None and candidate.path.endswith(READING_TAILS):
                remember(candidate, "")

    return list(found.values())


def describe(html: str) -> Shape:
    """열어 본 화면의 생김새. 목록형이면 몇 줄인지 센다."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    # 칸이 둘 이상인 줄만 센다. "조회할 자료가 없습니다"는 칸 하나를 늘려 쓰므로 저절로 빠진다.
    rows = [row for row in soup.select("tr") if len(row.select("td")) >= 2]
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


def data_path(html: str, path: str) -> str:
    """껍데기 화면이 따로 불러오는 내용 주소.

    목록 화면은 `..._list_form.acl`이 빈 틀만 주고, 줄은 `..._list.acl`을 다시 불러 채운다.
    파서가 읽어야 할 곳은 이쪽이므로 껍데기 안에서 그 주소가 실제로 불리는지 확인해 둔다.
    """
    if path.endswith(_SHELL_SUFFIX):
        guess = path[: -len(_SHELL_SUFFIX)] + SCREEN_SUFFIX
        if guess in html and safe_path(guess):
            return guess
    # 자기 자신에게 다시 요청해야 줄이 채워지는 화면도 있다 (쪽지함·학사일정)
    soup = BeautifulSoup(html, "html.parser")
    if any((form.get("action") or "").endswith(path) for form in soup.find_all("form")):
        return path
    return ""


def param_names(query: str) -> list[str]:
    """질의 문자열에서 이름만 꺼낸다. 값은 개인 정보일 수 있어 버린다."""
    return sorted(parse_qs(query, keep_blank_values=True))


def is_per_course(params: list[str]) -> bool:
    return any(name.lower() in _COURSE_PARAMS for name in params)


async def course_keys(session) -> list[str]:
    """수강 중인 과목의 방 열쇠. 할 일 화면의 과목 선택 상자에서 읽는다."""
    html = await session.open(TODO_PATH)
    return [row.kjkey for row in parse_course_select(html).rows]


async def explore(
    session,
    sample_dir: Path,
    *,
    seeds: tuple[str, ...] = SEEDS,
    courses: tuple[str, ...] = (),
    max_depth: int = MAX_DEPTH,
    max_screens: int = MAX_SCREENS,
    max_courses: int = MAX_COURSES,
    pause: float = PAUSE_SECONDS,
) -> list[Screen]:
    """메뉴를 따라가며 화면을 열어 보고 카탈로그를 만든다. 로그인은 부른 쪽이 해 둔다.

    두 바퀴를 돈다. 먼저 공통 화면을, 그다음 과목방 안을 본다.
    과목방은 어느 과목이나 메뉴가 같으므로 한 과목만 들어간다.
    """
    sample_dir.mkdir(parents=True, exist_ok=True)
    common: set[str] = set()
    screens = await _crawl(
        session,
        sample_dir,
        [(Candidate(name="", path=path), 0) for path in seeds],
        max_depth=max_depth,
        max_screens=max_screens,
        pause=pause,
        seen=common,
        skip_prefix=COURSE_PREFIX,
    )

    for key in list(courses)[:max_courses]:
        try:
            await session.enter_course(key)
        except EclassError as exc:
            logger.warning("과목방에 들어가지 못했습니다 (%s)", exc.reason)
            screens.append(
                Screen(name="과목방", path=COURSE_ROOM_PATH, per_course=True, note=f"들어가지 못함({exc.reason})")
            )
            continue
        screens += await _crawl(
            session,
            sample_dir,
            [(Candidate(name="과목방", path=COURSE_ROOM_PATH), 0)],
            max_depth=max_depth,
            max_screens=max_screens,
            pause=pause,
            per_course=True,
            prefix="course_",
            # 과목방에도 공통 머리글 메뉴가 그대로 들어 있다. 이미 본 것은 다시 열지 않는다.
            seen=common - {COURSE_ROOM_PATH},
        )

    return screens


async def _crawl(
    session,
    sample_dir: Path,
    queue: list[tuple[Candidate, int]],
    *,
    max_depth: int,
    max_screens: int,
    pause: float,
    per_course: bool = False,
    prefix: str = "",
    seen: set[str] | None = None,
    skip_prefix: str = "",
) -> list[Screen]:
    """대기줄을 비울 때까지 화면을 열어 본다. 같은 경로는 한 번만 연다.

    `seen`을 넘기면 그 경로는 건너뛰고, 새로 연 경로를 거기에 담아 돌려준다.
    """
    visited: set[str] = seen if seen is not None else set()
    screens: list[Screen] = []

    while queue and len(screens) < max_screens:
        candidate, depth = queue.pop(0)
        if candidate.path in visited or not safe_path(candidate.path):
            continue
        if skip_prefix and candidate.path.startswith(skip_prefix):
            # 여기서 열면 빈 껍데기만 남는다. 뒤에 과목방 안에서 연다.
            continue
        visited.add(candidate.path)

        try:
            html = await session.open(candidate.target)
        except EclassError as exc:
            logger.warning("열지 못함: %s (%s)", candidate.path, exc.reason)
            screens.append(
                Screen(
                    name=candidate.name,
                    path=candidate.path,
                    per_course=per_course,
                    note=f"열리지 않음({exc.reason})",
                )
            )
            continue

        params = param_names(candidate.query)
        shape = describe(html)
        sample = f"{prefix}{sample_name(candidate.path)}.html"
        (sample_dir / sample).write_text(html[:MAX_SAMPLE_BYTES], encoding="utf-8")

        # 껍데기라면 내용까지 받아 둔다. 줄 수와 생김새는 그쪽이 진짜다.
        inner = data_path(html, candidate.path)
        data_sample = ""
        note = ""
        if inner:
            try:
                data_html = await session.post(inner, {})
            except EclassError as exc:
                note = f"내용 주소를 받지 못함({exc.reason})"
            else:
                data_sample = f"{prefix}{sample_name(inner)}__data.html"
                (sample_dir / data_sample).write_text(data_html[:MAX_SAMPLE_BYTES], encoding="utf-8")
                shape = describe(data_html)

        screens.append(
            Screen(
                name=candidate.name,
                path=candidate.path,
                params=params,
                listing=shape.listing,
                items=shape.items,
                has_date=shape.has_date,
                has_due=shape.has_due,
                per_course=per_course or is_per_course(params),
                sample=sample,
                data_path=inner,
                data_sample=data_sample,
                note=note,
            )
        )
        logger.info("%s %s (%d줄)%s", "목록" if shape.listing else "화면", candidate.path, shape.items, f" ← {inner}" if inner else "")

        if depth < max_depth:
            for link in collect_links(html, with_scripts=depth == 0):
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
        target = screen.data_path or screen.path
        lines.append(f"  {screen.items:>3}줄  {screen.name or '(이름 없음)'} · {target} {marks}".rstrip())
    troubled = [screen for screen in screens if screen.note]
    if troubled:
        lines += ["", f"걸린 화면 {len(troubled)}개:"]
        lines += [f"  {screen.path} · {screen.note}" for screen in troubled]
    return "\n".join(lines)


async def main() -> int:
    # 윈도우 콘솔 기본 인코딩(cp949)으로는 한글 밖의 기호에서 멈춘다
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = load_settings().eclass
    if not settings.enabled:
        print("eClass 설정이 비어 있습니다. private/.env와 private/local.toml을 확인해 주세요.")
        return 1

    session = EclassSession(settings)
    await session.start()
    try:
        await session.ensure_login()
        keys = await course_keys(session)
        logger.info("수강 과목 %d개", len(keys))
        screens = await explore(session, SAMPLE_DIR, courses=tuple(keys))
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
