import asyncio
import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.admin.settings_service import RuntimeSettingsService
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import async_session_factory
from app.subscriptions.service import SubscriptionService
from app.services.admin_notification_service import notify_runtime

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
                return
            _last_run = now

            notification_days = await runtime.notification_days(settings.notification_days)
            service = SubscriptionService(session)
            reminders = await service.send_expiration_reminders(bot, notification_days)
            expired = await service.expire_due_subscriptions(bot)
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
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        scheduler.shutdown(wait=False)
        await notify_runtime(bot, "scheduler", "остановлен")
        if bot is not None:
            await bot.session.close()
