"""할 일 화면: 과제·시험·온라인 강의 기한.

마감이 있는 화면이라 알림 가치가 가장 높다. 수집기가 깨어날 때마다 본다.
마감이 바뀌면 한 번만 알린다 (이전 마감 값은 저장소가 기억한다).
"""

from datetime import datetime, timedelta
from typing import Any

from app.collectors.eclass.parse import CourseRow, TodoRow, parse_todo_list
from app.collectors.eclass.session import MAIN_PATH, TODO_PATH
from app.collectors.eclass.sources.base import SourceResult
from app.core.clock import format_kst
from app.core.events import Event, EventKind, EventSource
from app.storage.eclass import EclassItem, ItemChange

# 할 일 목록은 두 번에 나눠 받는다: 껍데기(과목 선택)를 먼저 열고, 그 안에서 목록을 부른다.
TODO_ROWS_PATH = "/ilos/mp/todo_list.acl"
TODO_FORM_DATA = {"TODO_CNT": "100", "encoding": "utf-8"}
TODO_ROWS_DATA = {"todoKjList": "", "chk_cate": "ALL", "encoding": "utf-8"}

# 이 시간 안에 닥친 마감은 즉시 알린다
URGENT_WINDOW = timedelta(hours=24)


class TodoSource:
    key = "todo"
    label = "할 일"
    # 수집기가 도는 주기를 그대로 따른다
    interval = timedelta(0)

    async def fetch(self, session: Any, courses: list[CourseRow]) -> SourceResult:
        # 할 일 껍데기를 먼저 열어야 그 안의 목록 요청이 받아들여진다
        await session.open(MAIN_PATH)
        await session.post(TODO_PATH, dict(TODO_FORM_DATA))
        html = await session.post(TODO_ROWS_PATH, dict(TODO_ROWS_DATA))
        result = parse_todo_list(html)
        return SourceResult([to_item(row) for row in result.rows], result.suspicious)

    def event_for(self, item: EclassItem, change: ItemChange, now: datetime) -> Event | None:
        if change is ItemChange.SAME:
            return None
        title = f"[{item.course}] {item.title}" if item.course else item.title

        if change is ItemChange.DUE_CHANGED:
            return Event(
                source=EventSource.ECLASS,
                kind=EventKind.DEADLINE_CHANGED,
                title=f"마감이 바뀌었습니다: {title}",
                body=f"{item.kind} · 새 마감 {format_kst(item.due_at)}" if item.due_at else f"{item.kind} · 마감 없음",
                urgent=True,
                due_at=item.due_at,
                ref_id=f"{item.item_id}:due:{item.due_at.isoformat() if item.due_at else 'none'}",
                meta={"course": item.course, "category": item.kind},
            )

        return Event(
            source=EventSource.ECLASS,
            kind=EventKind.DEADLINE if item.due_at else EventKind.NOTICE,
            title=title,
            body=item.kind,
            urgent=bool(item.due_at and item.due_at - now <= URGENT_WINDOW),
            due_at=item.due_at,
            ref_id=item.item_id,
            meta={"course": item.course, "category": item.kind},
        )


def to_item(row: TodoRow) -> EclassItem:
    return EclassItem(
        item_id=row.item_id,
        kind=row.category,
        title=row.title,
        course=row.course,
        due_at=row.due_at,
    )
