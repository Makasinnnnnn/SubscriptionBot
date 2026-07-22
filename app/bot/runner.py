import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand

from app.bot.handlers.admin import router as admin_router
from app.bot.handlers.admin_ui import router as admin_ui_router
from app.bot.handlers.identity import router as identity_router
from app.bot.handlers.legal import router as legal_router
from app.bot.handlers.start import router as start_router
from app.bot.handlers.subscriptions import router as subscriptions_router
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.services.admin_notification_service import notify_runtime

logger = logging.getLogger(__name__)


async def run_bot() -> None:
    setup_logging()
    settings = get_settings()
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is required to run the bot")

    bot = Bot(token=settings.bot_token)
    await _configure_bot_profile(bot)
    dispatcher = Dispatcher()
    dispatcher.include_router(identity_router)
    dispatcher.include_router(legal_router)
    dispatcher.include_router(admin_ui_router)
    dispatcher.include_router(admin_router)
    dispatcher.include_router(start_router)
    dispatcher.include_router(subscriptions_router)
    await notify_runtime(bot, "bot", "запущен")
    try:
        await dispatcher.start_polling(bot)
    finally:
        await notify_runtime(bot, "bot", "остановлен")
        await bot.session.close()


async def _configure_bot_profile(bot: Bot) -> None:
    settings = get_settings()
    try:
        await bot.set_my_description(description=settings.bot_description[:512])
        await bot.set_my_short_description(short_description=settings.bot_short_description[:120])
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Открыть главное меню"),
                BotCommand(command="support", description="Связаться с поддержкой"),
                BotCommand(command="terms", description="Условия использования"),
                BotCommand(command="privacy", description="Обработка данных"),
            ]
        )
    except TelegramAPIError as exc:
        logger.warning("Bot profile configuration failed error=%s", type(exc).__name__)
