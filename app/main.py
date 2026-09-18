"""비서 실행 진입점: 텔레그램 폴링 + 스케줄러.

실행: py -3.14 -m uv run python -m app.main
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Bot
from telegram.error import InvalidToken
from telegram.ext import Application, ApplicationBuilder

from app.agent.light import LightModel
from app.agent.loop import Assistant
from app.agent.memory import MarkdownMemoryStore
from app.agent.prompt import PromptBuilder, load_identity
from app.channels.telegram import TelegramNotifier
from app.collectors.weather import KmaWeather
from app.channels.telegram_bot import SERVICES_KEY, ChatHandlers, ChatServices
from app.core.clock import KST, utc_now
from app.core.config import ConfigError, Settings, load_settings
from app.core.interfaces import BriefingKind
from app.google.accounts import GoogleAccounts
from app.llm import LLM, create_llm
from app.scheduler.briefing import (
    BriefingService,
    CalendarBriefing,
    NewsBriefing,
    TaskBriefing,
    TodoBriefing,
    WeatherBriefing,
)
from app.scheduler.dispatcher import Dispatcher
from app.scheduler.gate import RuleBasedGate
from app.scheduler.tasks import TaskService
from app.storage.archive import ArchiveRepository
from app.storage.conversation import ConversationStore, PendingActionStore
from app.storage.location import LocationStore
from app.storage.db import Database
from app.storage.notifications import NotificationLog
from app.storage.tasks import TaskRepository
from app.storage.todos import TodoRepository
from app.tools.archive import archive_tools
from app.tools.calendar import calendar_tools, not_connected_tools
from app.tools.memory import memory_tools
from app.tools.registry import ToolRegistry
from app.tools.search import search_tools
from app.tools.tasks import task_tools
from app.tools.todos import todo_tools
from app.tools.weather import weather_tools

logger = logging.getLogger("app")

# 실시간 위치 공유는 edited_message로 들어온다
ALLOWED_UPDATES = ["message", "edited_message", "callback_query"]


@dataclass(slots=True)
class Runtime:
    db: Database
    scheduler: AsyncIOScheduler
    llm: LLM
    services: ChatServices
    http: httpx.AsyncClient

    async def close(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        await self.llm.close()
        await self.http.aclose()
        await self.db.close()


async def create_runtime(settings: Settings, bot: Bot, llm: LLM | None = None) -> Runtime:
    # 모델 제공사는 config.toml의 [llm] provider로 고른다. 인증·모델 ID는 여기서 먼저 확인된다.
    llm = llm or await create_llm(settings.llm)

    db = await Database.open(settings.storage.db_path)
    log = NotificationLog(db)
    todos = TodoRepository(db)
    conversation = ConversationStore(db)

    notifier = TelegramNotifier(bot, settings.telegram.allowed_user_id)
    dispatcher = Dispatcher(RuleBasedGate(log, settings.notification), log, notifier)
    scheduler = AsyncIOScheduler(timezone=KST)
    tasks = TaskService(TaskRepository(db), scheduler, dispatcher)

    # 외부 HTTP 호출(날씨 등)은 연결을 재사용한다. 종료할 때 함께 닫는다.
    http = httpx.AsyncClient(timeout=httpx.Timeout(10.0), headers={"Accept": "application/json"})
    # 날씨 기준 좌표: 사용자가 텔레그램으로 보낸 최근 위치 → 없으면 설정의 동네
    location = LocationStore(db)
    weather = KmaWeather(settings.weather, http, location=location)
    # Google 계정은 계정마다 1회 로그인(python -m app.google.login <이름>) 뒤부터 쓸 수 있다
    google = GoogleAccounts(settings.google, http)

    # 이름·호칭은 git에서 제외된 private/profile.md에서 읽는다
    honorific = load_identity(settings.storage.profile_path).honorific
    light = LightModel(llm.light, honorific)

    registry = ToolRegistry(PendingActionStore(db))
    memory = MarkdownMemoryStore(settings.storage.memory_path)
    archive = ArchiveRepository(db)
    registry.register(
        *todo_tools(todos),
        *memory_tools(memory),
        *task_tools(tasks),
        *weather_tools(weather),
        *search_tools(llm.search),
        *archive_tools(archive, http, light),
        *(calendar_tools(google) if google.ready else not_connected_tools()),
    )

    prompt = PromptBuilder(settings.storage.system_prompt_path, settings.storage.profile_path, memory)
    assistant = Assistant(llm.chat, settings.conversation, prompt, conversation, registry, light)
    tasks.set_agent_runner(assistant.run_task)

    briefing = BriefingService(
        [
            WeatherBriefing(weather),
            CalendarBriefing(google),
            TodoBriefing(todos),
            TaskBriefing(tasks),
            NewsBriefing(log),
        ],
        dispatcher,
        light,
        honorific,
    )
    _add_system_jobs(scheduler, settings, dispatcher, assistant, briefing)
    restored = await tasks.start()
    scheduler.start()
    logger.info("예약 작업 %d건 복원, 스케줄러 시작", restored)

    return Runtime(db, scheduler, llm, ChatServices(assistant, registry, conversation, location=location), http)


def _add_system_jobs(
    scheduler: AsyncIOScheduler,
    settings: Settings,
    dispatcher: Dispatcher,
    assistant: Assistant,
    briefing: BriefingService,
) -> None:
    def safe(name: str, job: Callable[[], Awaitable[object]]) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            try:
                await job()
            except Exception:
                logger.exception("시스템 작업 실패: %s", name)
        return run

    scheduler.add_job(
        safe("보류 알림 발송", lambda: dispatcher.release_pending(utc_now())),
        IntervalTrigger(minutes=1), id="system:release", coalesce=True, max_instances=1,
    )
    scheduler.add_job(
        safe("대화 압축", lambda: assistant.compact_if_idle(utc_now())),
        IntervalTrigger(minutes=5), id="system:compact", coalesce=True, max_instances=1,
    )
    for kind, at in ((BriefingKind.MORNING, settings.briefing.morning), (BriefingKind.EVENING, settings.briefing.evening)):
        scheduler.add_job(
            safe(f"{kind} 브리핑", lambda kind=kind: briefing.send(kind, utc_now())),
            CronTrigger(hour=at.hour, minute=at.minute, timezone=KST),
            id=f"system:briefing:{kind}", coalesce=True, max_instances=1, misfire_grace_time=1800,
        )
    # 주간 계획: 일요일 저녁에 다음 주를 정리한다
    weekly = settings.briefing.weekly
    scheduler.add_job(
        safe("주간 계획", lambda: briefing.send(BriefingKind.WEEKLY, utc_now())),
        CronTrigger(day_of_week="sun", hour=weekly.hour, minute=weekly.minute, timezone=KST),
        id="system:briefing:weekly", coalesce=True, max_instances=1, misfire_grace_time=1800,
    )


def build_application(settings: Settings) -> Application:
    async def post_init(application: Application) -> None:
        runtime = await create_runtime(settings, application.bot)
        application.bot_data["runtime"] = runtime
        application.bot_data[SERVICES_KEY] = runtime.services

    async def post_shutdown(application: Application) -> None:
        runtime: Runtime | None = application.bot_data.get("runtime")
        if runtime is not None:
            await runtime.close()

    application = (
        ApplicationBuilder()
        .token(settings.telegram.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    honorific = load_identity(settings.storage.profile_path).honorific
    ChatHandlers(settings.telegram.allowed_user_id, honorific).register(application)
    return application


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # 토큰이 들어간 요청 URL이 로그에 남지 않게 한다
    for noisy in ("httpx", "google_genai", "telegram.ext", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        application = build_application(load_settings())
        logger.info("비서를 시작합니다. 종료하려면 Ctrl+C를 누르세요.")
        application.run_polling(allowed_updates=ALLOWED_UPDATES)
    except ConfigError as exc:
        logger.error("설정 오류: %s", exc)
        raise SystemExit(1) from None
    except InvalidToken:
        # 이 예외의 메시지에는 토큰 원문이 들어 있어서 그대로 출력하지 않는다
        logger.error("텔레그램이 봇 토큰을 거부했습니다. private/.env의 TELEGRAM_BOT_TOKEN을 확인하세요.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
