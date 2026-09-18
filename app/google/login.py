"""Google 계정 연결 (계정마다 1회 실행).

실행: py -3.14 -m uv run python -m app.google.login <계정 이름>
이름을 빼면 연결 상태만 보여 준다. 브라우저가 열리면 계정을 고르고 권한을 허용하면 된다.
토큰은 계정마다 private/google_token_N.json에 따로 저장된다.
"""

import asyncio
import logging
import sys
import webbrowser

import httpx

from app.core.config import ConfigError, load_settings
from app.google.accounts import GoogleAccounts
from app.google.auth import GoogleAuthError

logger = logging.getLogger("app.google.login")


async def run(label: str | None) -> None:
    settings = load_settings().google
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as http:
        accounts = GoogleAccounts(settings, http)
        print("연결 상태")
        print(accounts.status())
        if label is None:
            print()
            print("연결하려면: py -3.14 -m uv run python -m app.google.login <계정 이름>")
            return
        account = accounts.find(label)
        if account is None:
            raise GoogleAuthError(f"그런 계정 이름이 없습니다: {label} (설정된 이름: {', '.join(accounts.labels)})")

        print()
        print(f"'{account.label}' 계정으로 로그인합니다. 브라우저에서 계정을 고르고 권한을 허용해 주세요.")
        print("'확인되지 않은 앱' 경고가 나오면 고급 → 계속을 누르시면 됩니다.")
        token = await account.auth.login(webbrowser.open)
    print(f"연결이 끝났습니다: {account.label}")
    print("허용된 권한:", ", ".join(scope.rsplit("/", 1)[-1] for scope in token.scopes))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(run(sys.argv[1] if len(sys.argv) > 1 else None))
    except (ConfigError, GoogleAuthError) as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
