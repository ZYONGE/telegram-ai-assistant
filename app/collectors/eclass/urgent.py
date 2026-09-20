"""급한 소식 판정: 이 공지를 지금 알려야 하는가.

마감이 코앞인 것은 날짜로 알 수 있지만, 휴강·보강·시험 일정 변경은 글을 읽어야 안다.
모델을 부르지 않고 **코드 규칙**으로만 가린다. 낱말 목록은 `config.toml`에 둔다 (개인정보가 아니다).

**애매하면 즉시 알리지 않는다.** 놓치는 것보다 잘못 울리는 쪽이 더 성가시다 (CLAUDE.md 7절).
즉시 알린다고 해도 조용한 시간(23:00~06:30)은 그대로 지킨다. 그건 게이트가 막는다.
"""

from app.core.config import DEFAULT_URGENT_WORDS

__all__ = ["BODY_WINDOW", "DEFAULT_URGENT_WORDS", "is_urgent"]

# 본문은 길다. 앞부분만 본다 (요지는 대개 처음에 온다).
BODY_WINDOW = 300


def is_urgent(title: str, body: str = "", words: tuple[str, ...] = DEFAULT_URGENT_WORDS) -> bool:
    """제목과 본문 앞부분에서 급한 낱말을 찾는다. 글자는 데이터로만 다룬다 (절대 규칙 8)."""
    if not words:
        return False
    haystack = f"{title} {body[:BODY_WINDOW]}"
    return any(word in haystack for word in words)
