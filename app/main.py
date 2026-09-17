"""2단계 확인용 1회 실행: 가짜 수집기 → 할 일 등록 → 알림 게이트 → 텔레그램.

실행: py -3.14 -m uv run python -m app.main
"""

import asyncio
import logging

from telegram import Bot
from telegram.error import InvalidToken

from app.channels.telegram import TelegramNotifier
from app.collectors.fake import FakeCollector, sample_events
from app.core.clock import utc_now
from app.core.config import ConfigError, Settings, load_settings
from app.scheduler.dispatcher import Dispatcher
from app.scheduler.gate import RuleBasedGate
from app.scheduler.ingest import Ingestor
from app.storage.db import Database
from app.storage.notifications import NotificationLog
from app.storage.todos import TodoRepository

logger = logging.getLogger("app")


async def run_once(settings: Settings) -> None:
    now = utc_now()
    db = await Database.open(settings.storage.db_path)
    try:
        log = NotificationLog(db)
        todos = TodoRepository(db)
        async with Bot(settings.telegram.bot_token) as bot:
            notifier = TelegramNotifier(bot, settings.telegram.allowed_user_id)
            dispatcher = Dispatcher(RuleBasedGate(log, settings.notification), log, notifier)
            released = await dispatcher.release_pending(now)
            decisions = await Ingestor(todos, dispatcher).run_collector(FakeCollector(sample_events(now)), now)
        logger.info("보류 해제 발송 %d건", released)
        for decision in decisions:
            logger.info("게이트 결정: %s (%s)", decision.action.value, decision.reason)
        logger.info("열린 할 일 %d건", len(await todos.list_open()))
    finally:
        await db.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # 토큰이 들어간 요청 URL이 로그에 남지 않게 한다
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run_once(load_settings()))
    except ConfigError as exc:
        logger.error("설정 오류: %s", exc)
        raise SystemExit(1) from None
    except InvalidToken:
        # 이 예외의 메시지에는 토큰 원문이 들어 있어서 그대로 출력하지 않는다
        logger.error("텔레그램이 봇 토큰을 거부했습니다. .env의 TELEGRAM_BOT_TOKEN을 확인하세요.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
