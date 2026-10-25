import asyncio
import logging
import signal
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.admin.settings_service import RuntimeSettingsService
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import async_session_factory
from app.services.access_operation_service import AccessOperationService
from app.services.admin_notification_service import notify_runtime
from app.subscriptions.service import SubscriptionService

logger = logging.getLogger(__name__)
_last_run: datetime | None = None


async def expire_subscriptions_job(bot: Bot | None = None) -> None:
    global _last_run
    settings = get_settings()
    try:
        async with async_session_factory() as session:
            runtime = RuntimeSettingsService(session)
            interval = await runtime.scheduler_interval_minutes(settings.scheduler_interval_minutes)
            now = datetime.now(UTC)
            if _last_run is not None and now - _last_run < timedelta(minutes=interval):
                await AccessOperationService(session).process_pending(bot)
                return

            notification_intervals = await runtime.notification_intervals_minutes(
                settings.notification_intervals_minutes,
                settings.notification_days,
            )
            service = SubscriptionService(session)
            expired = await service.expire_due_subscriptions(bot, defer_telegram=True)
            _last_run = now if expired < 100 else None
            await AccessOperationService(session).process_pending(bot)
            reminders = await service.send_expiration_reminders(
                bot,
                notification_intervals,
            )
            logger.info(
                "Subscription scheduler completed reminders=%s expired=%s",
                reminders,
                expired,
            )
    except Exception as exc:
        await notify_runtime(bot, "scheduler", "критическая ошибка", type(exc).__name__)
        raise


async def run_scheduler() -> None:
    setup_logging()
    settings = get_settings()
    bot = Bot(token=settings.bot_token) if settings.bot_token else None
    scheduler = AsyncIOScheduler()
    scheduler.add_job(expire_subscriptions_job, "interval", minutes=1, args=[bot])
    scheduler.start()
    await notify_runtime(bot, "scheduler", "запущен")
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous_handlers = {}
    loop_signals = set()
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        try:
            loop.add_signal_handler(signum, stopped.set)
            loop_signals.add(signum)
        except NotImplementedError:
            signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stopped.set))
    try:
        await stopped.wait()
    finally:
        for signum, handler in previous_handlers.items():
            if signum in loop_signals:
                loop.remove_signal_handler(signum)
            signal.signal(signum, handler)
        scheduler.shutdown(wait=False)
        await notify_runtime(bot, "scheduler", "остановлен")
        if bot is not None:
            await bot.session.close()
