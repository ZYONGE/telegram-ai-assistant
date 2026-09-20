"""강의계획서 소스.

학기에 한 번 바뀌는 정적 문서라 하루에 한 번만 읽는다. 알리지 않고 저장만 해 두었다가
"이 과목 평가 어떻게 돼?", "교재 뭐였지?" 같은 물음에 답하는 데 쓴다 (CLAUDE.md 6절).

게시판이 아니라 표로 된 문서라 목록 파서로는 읽히지 않는다. 그래서 소스를 따로 둔다.
교수 연락처가 들어 있으므로 `private/` 안 DB에만 두고 로그에 남기지 않는다 (절대 규칙 12).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.collectors.eclass.parse import CourseRow, parse_syllabus
from app.collectors.eclass.session import EclassError
from app.collectors.eclass.sources.base import SourceResult
from app.core.config import Level
from app.core.events import Event
from app.storage.eclass import EclassItem, ItemChange

logger = logging.getLogger(__name__)

# 학기에 한 번 바뀌는 문서다. 자주 볼 까닭이 없다.
DEFAULT_INTERVAL = timedelta(hours=24)
# 이 수만큼도 못 읽으면 화면이 바뀐 것으로 본다
MIN_PAIRS = 3


@dataclass(frozen=True, slots=True)
class SyllabusSource:
    key: str = "course_plan"
    label: str = "강의계획서"
    path: str = "/ilos/st/course/plan_form.acl"
    level: Level = Level.STORE
    per_course: bool = True
    interval: timedelta = DEFAULT_INTERVAL
    priority: int = 2

    async def fetch(self, session: Any, courses: list[CourseRow]) -> SourceResult:
        items: list[EclassItem] = []
        read = 0
        for course in courses:
            try:
                await session.enter_course(course.kjkey)
                pairs = parse_syllabus(await session.open(self.path))
            except EclassError as exc:
                logger.warning("강의계획서를 읽지 못했습니다 (%s)", exc.reason)
                continue
            read += 1
            if len(pairs) < MIN_PAIRS:
                continue  # 아직 올라오지 않은 과목
            items.append(
                EclassItem(
                    item_id=f"eclass:{self.key}:{course.kjkey}",
                    kind=self.label,
                    title=f"{course.name} 강의계획서",
                    course=course.name,
                    body="\n".join(f"{name}: {value}" for name, value in pairs),
                    source=self.key,
                )
            )
        return SourceResult(items, suspicious=bool(courses) and read > 0 and not items)

    def event_for(self, item: EclassItem, change: ItemChange, now: datetime) -> Event | None:
        # 알리지 않는다. 물어보면 eclass_search가 찾아 준다.
        return None
