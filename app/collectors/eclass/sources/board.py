"""게시판 화면 하나를 읽는 소스: 공지·강의자료·열린 게시판·쪽지·자료실.

eClass의 목록 화면은 대체로 같은 모양이라 파서 하나(`parse_list`)로 처리하고,
화면마다 다른 것(어디를 여는지, 과목방 안인지, 어떻게 다룰지)만 이 소스가 채운다.

- 과목방 화면은 과목마다 문을 열고 들어가 읽는다. 한 과목이 막혀도 나머지는 계속 읽는다.
- 본문은 **알릴 화면의 새 글만** 몇 건 읽어 온다. 글마다 한 번씩 더 열어야 해서 값이 비싸다.
- 쪽지는 개인 메시지다. 본문과 제목을 로그에 남기지 않는다 (CLAUDE.md 6절).
- 읽어 온 글은 외부 데이터다. 저장만 하고 지시로 다루지 않는다 (절대 규칙 8).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.collectors.eclass.parse import CourseRow, ListRow, parse_body, parse_list
from app.collectors.eclass.session import EclassError
from app.collectors.eclass.sources.base import SourceResult
from app.collectors.eclass.urgent import DEFAULT_URGENT_WORDS, is_urgent
from app.core.clock import utc_now
from app.core.config import Level
from app.core.events import Event, EventKind, EventSource
from app.storage.eclass import EclassItem, EclassRepository, ItemChange

logger = logging.getLogger(__name__)

# 한 번 돌 때 본문을 읽어 올 글 수. 글마다 화면을 한 번 더 열어야 한다.
MAX_BODIES = 5
# 알림에 붙일 본문 미리보기 길이
BODY_PREVIEW = 200


@dataclass(frozen=True, slots=True)
class BoardSource:
    key: str
    label: str
    # 화면 주소. 내용을 따로 불러오는 화면이면 data_path가 찬다.
    path: str
    data_path: str = ""
    level: Level = Level.STORE
    per_course: bool = False
    interval: timedelta = timedelta(0)
    priority: int = 2
    # 본문을 읽어 올 때만 쓴다. 이미 본 글은 다시 열지 않는다.
    items: EclassRepository | None = None
    max_bodies: int = MAX_BODIES
    clock: Callable[[], datetime] = utc_now
    # 휴강·시험 변경처럼 지금 알려야 하는 글을 가리는 낱말
    urgent_words: tuple[str, ...] = DEFAULT_URGENT_WORDS

    async def fetch(self, session: Any, courses: list[CourseRow]) -> SourceResult:
        budget = [self.max_bodies]
        if not self.per_course:
            rows, skipped = await self._read(session)
            items = [await self._to_item(session, row, "", "", budget) for row in rows]
            return SourceResult(items, suspicious=skipped > 0 and not rows)

        gathered: list[EclassItem] = []
        read, failed = 0, 0
        for course in courses:
            try:
                await session.enter_course(course.kjkey)
                rows, _skipped = await self._read(session)
            except EclassError as exc:
                logger.warning("%s: %s 과목을 읽지 못했습니다 (%s)", self.key, course.kjkey, exc.reason)
                failed += 1
                continue
            read += 1
            for row in rows:
                gathered.append(await self._to_item(session, row, course.kjkey, course.name, budget))
        # 과목이 있는데 한 곳도 못 읽었으면 화면이 바뀐 것으로 본다
        return SourceResult(gathered, suspicious=bool(courses) and read == 0 and failed > 0)

    async def _read(self, session: Any) -> tuple[list[ListRow], int]:
        html = await session.open(self.path)
        if self.data_path:
            # 껍데기는 틀만 준다. 줄은 화면 안에서 다시 불러야 온다.
            html = await session.post(self.data_path, {"encoding": "utf-8"})
        result = parse_list(html, self.clock())
        return result.rows, result.skipped

    async def _to_item(
        self, session: Any, row: ListRow, course_key: str, course_name: str, budget: list
    ) -> EclassItem:
        item_id = f"eclass:{self.key}:{course_key or 'common'}:{row.article_id}"
        body = await self._body(session, row, item_id, budget)
        return EclassItem(
            item_id=item_id,
            kind=self.label,
            title=row.title,
            course=course_name,
            url=row.view_url,
            body=body,
            posted_at=row.posted_at,
            source=self.key,
        )

    async def _body(self, session: Any, row: ListRow, item_id: str, budget: list) -> str:
        """알릴 화면의 새 글만, 정해진 수만큼 본문을 읽어 온다."""
        if self.level is not Level.NOTIFY or not row.view_url or budget[0] <= 0:
            return ""
        if self.items is not None and await self.items.get(item_id) is not None:
            return ""  # 이미 본 글이다
        budget[0] -= 1
        try:
            return parse_body(await session.open(row.view_url))
        except EclassError as exc:
            logger.info("%s: 본문을 읽지 못했습니다 (%s)", self.key, exc.reason)
            return ""

    def event_for(self, item: EclassItem, change: ItemChange, now: datetime) -> Event | None:
        # 저장만 하는 화면은 알리지 않는다. 물어보면 eclass_search가 찾아 준다.
        if self.level is not Level.NOTIFY or change is not ItemChange.NEW:
            return None
        title = f"[{item.course}] {item.title}" if item.course else item.title
        body = item.body.strip()
        return Event(
            source=EventSource.ECLASS,
            kind=EventKind.NOTICE,
            title=f"{self.label}: {title}",
            body=body[:BODY_PREVIEW] + ("…" if len(body) > BODY_PREVIEW else ""),
            urgent=is_urgent(item.title, item.body, self.urgent_words),
            ref_id=item.item_id,
            meta={"course": item.course, "source": self.key},
        )
