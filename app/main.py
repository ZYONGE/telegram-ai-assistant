"""비서 실행 진입점: 텔레그램 폴링 + 스케줄러.

실행: py -3.14 -m uv run python -m app.main
"""

import asyncio
import logging
from logging.handlers import RotatingFileHandler
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Bot
from telegram.error import InvalidToken
from telegram.error import NetworkError
from telegram.ext import Application, ApplicationBuilder

from app.agent.light import LightModel
from app.agent.loop import Assistant
from app.agent.memory import MarkdownMemoryStore
from app.agent.prompt import PromptBuilder, clean_profile, load_identity
from app.channels.telegram import TelegramNotifier
from app.collectors.eclass.browse import EclassBrowser
from app.collectors.eclass.collector import EclassCollector
from app.collectors.eclass.scope import ScopeStore, ensure_scope
from app.collectors.eclass.sources import build_sources
from app.collectors.mail import MailCollector
from app.mail.mailbox import Mailbox
from app.mail.rules import AutoPolicy
from app.collectors.weather import KmaWeather
from app.channels.telegram_bot import SERVICES_KEY, ChatHandlers, ChatServices
from app.core.clock import KST, to_kst, utc_now
from app.core.config import LoggingSettings, ConfigError, NotificationSettings, Settings, load_settings
from app.core.interfaces import BriefingKind
from app.google.accounts import GoogleAccounts
from app.mail.service import MailService
from app.llm import LLM, create_llm
from app.scheduler.briefing import (
    BriefingService,
    CalendarBriefing,
    MailBriefing,
    NewsBriefing,
    TaskBriefing,
    TodoBriefing,
    WeatherBriefing,
)
from app.scheduler.deadlines import CHECK_EVERY, DeadlineReminder
from app.scheduler.dispatcher import Dispatcher
from app.scheduler.gate import RuleBasedGate
from app.scheduler.ingest import Ingestor
from app.scheduler.tasks import TaskService
from app.storage.archive import ArchiveRepository
from app.storage.backup import BackupService
from app.storage.conversation import ConversationStore, PendingActionStore
from app.storage.eclass import EclassHealthStore, EclassRepository, EclassSourceStateStore
from app.storage.location import LocationStore
from app.storage.mail import MailCleanupLog, MailRuleRepository, MailStateStore, WaitingReplyStore
from app.storage.db import Database
from app.storage.notifications import NotificationLog
from app.storage.tasks import TaskRepository
from app.storage.todos import TodoRepository
from app.tools.archive import archive_tools
from app.tools.calendar import calendar_tools, not_connected_tools
from app.tools.eclass import eclass_tools
from app.tools.eclass_browse import eclass_browse_tools
from app.tools.mailbox import mailbox_tools, not_connected_mailbox_tools
from app.tools.mail import mail_tools, not_connected_mail_tools
from app.tools.memory import memory_tools
from app.tools.registry import ToolRegistry
from app.tools.search import search_tools
from app.tools.tasks import task_tools
from app.tools.todos import todo_tools
from app.tools.weather import weather_tools

logger = logging.getLogger("app")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

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
    gate = RuleBasedGate(log, settings.notification)
    # 이름·호칭은 git에서 제외된 private/profile.md에서, 말투·보고 방식은 private/instructions.md에서 읽는다
    honorific = load_identity(settings.storage.profile_path).honorific
    light = LightModel(llm.light, honorific, style=_style_reader(settings.storage.instructions_path))
    # 선제 알림은 보낼 때 사용자가 정한 말투로 다시 쓴다
    dispatcher = Dispatcher(gate, log, notifier, phraser=light)
    scheduler = AsyncIOScheduler(timezone=KST)
    tasks = TaskService(TaskRepository(db), scheduler, dispatcher)

    # 외부 HTTP 호출(날씨 등)은 연결을 재사용한다. 종료할 때 함께 닫는다.
    http = httpx.AsyncClient(timeout=httpx.Timeout(10.0), headers={"Accept": "application/json"})
    # 날씨 기준 좌표: 사용자가 텔레그램으로 보낸 최근 위치 → 없으면 설정의 동네
    location = LocationStore(db)
    weather = KmaWeather(settings.weather, http, location=location)
    # Google 계정은 계정마다 1회 로그인(python -m app.google.login <이름>) 뒤부터 쓸 수 있다
    google = GoogleAccounts(settings.google, http)
    mail_rules = MailRuleRepository(db)
    mail_state = MailStateStore(db)
    mail_cleanup = MailCleanupLog(db)
    waiting_replies = WaitingReplyStore(db)
    mail = MailService(google, mail_cleanup, waiting_replies, mail_state)


    registry = ToolRegistry(PendingActionStore(db))
    eclass_scope = ScopeStore(settings.eclass.scope_file)
    eclass_items = EclassRepository(db)
    eclass_health = EclassHealthStore(db)
    # 수집기와 그 자리 조회가 같은 세션 쿠키를 쓴다. 과목방 문을 여닫는 순서가 섞이지 않게 잠금 하나를 나눠 쓴다.
    eclass_lock = asyncio.Lock()
    eclass_browser = EclassBrowser(settings.eclass, eclass_lock, eclass_health)
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
        *(mail_tools(google, mail_rules, mail) if google.ready else not_connected_mail_tools()),
        *(mailbox_tools(google, Mailbox(google)) if google.ready else not_connected_mailbox_tools()),
        *(eclass_tools(eclass_scope, eclass_items) if settings.eclass.enabled else ()),
        *(eclass_browse_tools(eclass_browser) if settings.eclass.enabled else ()),
    )

    prompt = PromptBuilder(
        settings.storage.system_prompt_path,
        settings.storage.profile_path,
        memory,
        settings.storage.instructions_path,
    )
    assistant = Assistant(llm.chat, settings.conversation, prompt, conversation, registry, light)
    tasks.set_agent_runner(assistant.run_task)

    briefing = BriefingService(
        [
            WeatherBriefing(weather),
            CalendarBriefing(google),
            MailBriefing(mail),
            TodoBriefing(todos),
            TaskBriefing(tasks),
            NewsBriefing(log),
        ],
        dispatcher,
        light,
        honorific,
    )
    eclass_collector = EclassCollector(
        settings.eclass, eclass_items, eclass_health, EclassSourceStateStore(db), lock=eclass_lock
    )
    if settings.eclass.enabled:
        # 탐색기가 만든 화면 목록을 보고 무엇을 가져올지 정한다. 새 화면이 생겼을 때만 다시 정한다.
        scope = await ensure_scope(settings.eclass.catalog_file, settings.eclass.scope, eclass_scope, light)
        eclass_collector.set_sources(
            build_sources(
                settings.eclass.catalog_file, scope, eclass_items, settings.eclass.urgent_words
            )
        )
    school = school_domains(settings)
    mail_collector = MailCollector(
        google,
        mail_rules,
        mail_state,
        mail_cleanup,
        waiting_replies,
        frozenset(settings.mail.protected_domains) | school,
        auto=AutoPolicy(settings.mail.auto, school, settings.mail.corporate_to_spam),
        receipt_label=settings.mail.receipt_label,
    )
    ingestor = Ingestor(todos, dispatcher)
    backup = BackupService(db, settings.storage.db_path.parent, settings.backup)
    deadlines = DeadlineReminder(todos, dispatcher, gate, settings.notification.deadline_reminders)
    _add_system_jobs(scheduler, settings, dispatcher, assistant, briefing, deadlines)
    _add_collector_jobs(scheduler, settings, ingestor, mail_collector, google, eclass_collector)
    _add_backup_job(scheduler, settings, ingestor, backup)
    restored = await tasks.start()
    scheduler.start()
    logger.info("예약 작업 %d건 복원, 스케줄러 시작", restored)

    services = ChatServices(assistant, registry, conversation, location=location, mail=mail, voice=light)
    return Runtime(db, scheduler, llm, services, http)


def school_domains(settings: Settings) -> frozenset[str]:
    """학교 메일 도메인. 설정에 없으면 eClass 주소에서 짐작한다 (eclass.학교.ac.kr → 학교.ac.kr).

    학교를 특정하는 값이라 config.toml에 적지 않는다. eClass 주소는 이미 private/local.toml에 있다.
    """
    if settings.mail.school_domains:
        return frozenset(settings.mail.school_domains)
    host = (urlsplit(settings.eclass.eclass_url).hostname or "").lower()
    parts = host.split(".")
    return frozenset({".".join(parts[1:])}) if len(parts) >= 3 else frozenset()


def _style_reader(path) -> Callable[[], str]:
    """지시 파일을 읽어 모델에 넘길 부분만 남긴다. 부를 때마다 읽어 고친 내용이 바로 적용된다."""

    def read() -> str:
        if path is None or not path.exists():
            return ""
        return clean_profile(path.read_text(encoding="utf-8"))

    return read


def _add_system_jobs(
    scheduler: AsyncIOScheduler,
    settings: Settings,
    dispatcher: Dispatcher,
    assistant: Assistant,
    briefing: BriefingService,
    deadlines: DeadlineReminder | None = None,
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
    if deadlines is not None:
        # 마감 전 정해 둔 시점마다 한 번씩. 시점보다 늦지 않게, 확인 간격만큼 일찍 알아챈다.
        scheduler.add_job(
            safe("마감 리마인더", lambda: deadlines.run(utc_now())),
            IntervalTrigger(seconds=CHECK_EVERY.total_seconds()), id="system:deadlines", coalesce=True, max_instances=1,
        )
    # 주간 계획: 일요일 저녁에 다음 주를 정리한다
    weekly = settings.briefing.weekly
    scheduler.add_job(
        safe("주간 계획", lambda: briefing.send(BriefingKind.WEEKLY, utc_now())),
        CronTrigger(day_of_week="sun", hour=weekly.hour, minute=weekly.minute, timezone=KST),
        id="system:briefing:weekly", coalesce=True, max_instances=1, misfire_grace_time=1800,
    )


def _add_backup_job(
    scheduler: AsyncIOScheduler,
    settings: Settings,
    ingestor: Ingestor,
    backup: BackupService,
) -> None:
    """매일 밤 백업. 실패하면 조용히 넘어가지 않고 알린다."""
    if not settings.backup.enabled:
        logger.info("백업이 꺼져 있습니다")
        return

    async def run_backup() -> None:
        now = utc_now()
        events = await backup.run(now)
        if events:
            await ingestor.ingest(events, now)

    at = settings.backup.at
    scheduler.add_job(
        run_backup,
        CronTrigger(hour=at.hour, minute=at.minute, timezone=KST),
        id="system:backup",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    logger.info("백업: 매일 %02d:%02d (%d일 보관)", at.hour, at.minute, settings.backup.keep_days)


def _add_collector_jobs(
    scheduler: AsyncIOScheduler,
    settings: Settings,
    ingestor: Ingestor,
    mail_collector: MailCollector,
    google: GoogleAccounts,
    eclass_collector: EclassCollector | None = None,
) -> None:
    """수집기 주기 작업.

    수집기는 서로 독립이다. **한쪽이 꺼져 있어도 다른 쪽은 등록되어야 한다** —
    예전에는 메일 조건에서 일찍 빠져나가는 바람에 Google을 연결하지 않으면
    eClass 수집까지 함께 꺼졌다.
    """
    _add_mail_job(scheduler, settings, ingestor, mail_collector, google)
    _add_eclass_job(scheduler, settings, ingestor, eclass_collector)


def _add_mail_job(
    scheduler: AsyncIOScheduler,
    settings: Settings,
    ingestor: Ingestor,
    mail_collector: MailCollector,
    google: GoogleAccounts,
) -> None:
    """메일 수집. 조용한 시간에는 돌리지 않는다 (알림도 어차피 보류된다)."""
    minutes = settings.mail.poll_minutes
    if minutes <= 0 or not google.ready:
        logger.info("메일 수집을 켜지 않았습니다 (설정 %d분, 계정 연결 %s)", minutes, google.ready)
        return

    async def collect_mail() -> None:
        try:
            await ingestor.run_collector(mail_collector, utc_now())
        except Exception:
            logger.exception("메일 수집 실패")

    start, end = settings.notification.quiet_end, settings.notification.quiet_start
    scheduler.add_job(
        collect_mail,
        CronTrigger(minute=f"*/{minutes}", hour=f"{start.hour}-{end.hour}", timezone=KST),
        id="collector:mail",
        coalesce=True,
        max_instances=1,
    )
    logger.info("메일 수집: %d분마다 (%02d시~%02d시)", minutes, start.hour, end.hour)


def _add_eclass_job(
    scheduler: AsyncIOScheduler,
    settings: Settings,
    ingestor: Ingestor,
    eclass_collector: EclassCollector | None,
) -> None:
    """eClass 수집. 학교 주소와 계정만 있으면 켜지고, Google 연동과는 상관이 없다."""
    if eclass_collector is None or not settings.eclass.enabled:
        logger.info("eClass 수집을 켜지 않았습니다 (주소와 계정이 필요합니다)")
        return

    async def collect_eclass() -> None:
        now = utc_now()
        if not within_active_hours(now, settings.notification):
            return  # 조용한 시간에는 돌리지 않는다
        try:
            await ingestor.run_collector(eclass_collector, now)
        except Exception:
            logger.exception("eClass 수집 실패")

    scheduler.add_job(
        collect_eclass,
        IntervalTrigger(minutes=settings.eclass.poll_minutes),
        id="collector:eclass",
        coalesce=True,
        max_instances=1,
    )
    start, end = settings.notification.quiet_end, settings.notification.quiet_start
    logger.info("eClass 수집: %d분마다 (%02d시~%02d시)", settings.eclass.poll_minutes, start.hour, end.hour)


def within_active_hours(now: datetime, notification: NotificationSettings) -> bool:
    """조용한 시간(기본 23:00~06:30) 밖이면 True."""
    local = to_kst(now).time()
    start, end = notification.quiet_end, notification.quiet_start
    return start <= local < end if start <= end else not (end <= local < start)


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
    application.add_error_handler(on_telegram_error)
    return application


async def on_telegram_error(_update: object, context: object) -> None:
    """텔레그램 쪽에서 난 오류를 한 줄로 남긴다.

    처리기를 걸지 않으면 재시도마다 스택 추적이 통째로 찍힌다. 인터넷이 끊기면
    몇 초 간격으로 계속 재시도하므로 로그가 그것으로 가득 찬다 (docs/tasks.md T-28).
    연결 문제는 라이브러리가 알아서 다시 붙으므로 한 줄이면 된다.
    """
    error = getattr(context, "error", None)
    if isinstance(error, NetworkError):
        logger.warning("텔레그램에 연결하지 못했습니다 (%s). 다시 붙습니다.", type(error).__name__)
        return
    logger.error("텔레그램 처리 중 오류: %s", type(error).__name__, exc_info=error)


def setup_logging(settings: LoggingSettings) -> None:
    """화면에는 늘 남기고, 정해져 있으면 파일에도 남긴다.

    파일은 정해진 크기에서 넘어가며 몇 개만 남는다. 로그가 디스크를 채우지 않게 한다.
    """
    root = logging.getLogger()
    if settings.file is None:
        return
    try:
        settings.file.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            settings.file,
            maxBytes=settings.max_mb * 1024 * 1024,
            backupCount=settings.backups,
            encoding="utf-8",
        )
    except OSError as exc:
        # 파일에 못 남긴다고 비서가 안 뜰 까닭은 없다
        logger.warning("로그 파일을 열지 못했습니다 (%s). 화면에만 남깁니다.", type(exc).__name__)
        return
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(handler)
    logger.info("로그 파일: %s (%dMB씩 %d개)", settings.file.name, settings.max_mb, settings.backups)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    # 토큰이 들어간 요청 URL이 로그에 남지 않게 한다
    for noisy in ("httpx", "google_genai", "telegram.ext", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        settings = load_settings()
        setup_logging(settings.logging)
        application = build_application(settings)
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
