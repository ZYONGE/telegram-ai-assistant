"""수집 소스의 약속.

eClass에는 볼 화면이 여러 가지다 (할 일, 과목 공지, 쪽지, 학사일정, 강의계획서).
화면마다 여는 방법과 읽는 방법이 다르므로 **화면 하나에 소스 하나**를 둔다.
수집기는 켜진 소스를 돌리고 결과를 모으는 역할만 한다.

- `key`: 소스 이름. 주기 기록과 수집 범위 판정(T-07)이 이 이름으로 맞춰진다.
- `label`: 사용자에게 보일 이름. 어느 화면이 막혔는지 알릴 때 쓴다.
- `interval`: 이 화면을 얼마 만에 한 번 볼지. `timedelta(0)`이면 수집기가 도는 주기를 그대로 따른다.
- `fetch`: 화면을 열어 우리 자료형(`EclassItem`)으로 바꾼다. 읽은 글자는 데이터일 뿐이다 (절대 규칙 8).
- `event_for`: 저장소가 알려 준 변화를 알릴 이벤트로 바꾼다. 알릴 것이 없으면 None.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from app.collectors.eclass.parse import CourseRow
from app.core.events import Event
from app.storage.eclass import EclassItem, ItemChange


@dataclass(frozen=True, slots=True)
class SourceResult:
    items: list[EclassItem] = field(default_factory=list)
    # 형태가 달라 한 줄도 읽지 못했다. 사이트 구조가 바뀐 것으로 본다.
    suspicious: bool = False


class EclassSource(Protocol):
    key: str
    label: str
    interval: timedelta

    async def fetch(self, session: Any, courses: list[CourseRow]) -> SourceResult:
        """화면을 열어 항목 목록을 만든다. 실패는 EclassError로 알린다.

        `courses`는 과목방마다 따로 있는 화면(공지·자료실)을 위한 것이다.
        과목 목록 확보는 T-06 탐색 뒤 T-09에서 붙인다. 그때까지는 빈 목록이 들어온다.
        """
        ...

    def event_for(self, item: EclassItem, change: ItemChange, now: datetime) -> Event | None:
        """처음 본 항목인지, 무엇이 바뀌었는지에 따라 알릴 내용을 만든다."""
        ...
