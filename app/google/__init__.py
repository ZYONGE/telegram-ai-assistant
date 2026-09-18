"""Google API 호출.

Google 계정 인증과 API 요청은 이 폴더 안에서만 한다 (app/llm과 같은 방식).
바깥에서는 여기서 정의한 클라이언트만 쓰고, 토큰과 클라이언트 파일은 private/ 안에만 둔다.
"""

from app.google.auth import (
    CALENDAR_SCOPE,
    GMAIL_SCOPE,
    GoogleApiError,
    GoogleAuth,
    GoogleAuthError,
    LOGIN_NEEDED,
    TransientGoogleError,
)

__all__ = [
    "CALENDAR_SCOPE",
    "GMAIL_SCOPE",
    "LOGIN_NEEDED",
    "GoogleApiError",
    "GoogleAuth",
    "GoogleAuthError",
    "TransientGoogleError",
]
