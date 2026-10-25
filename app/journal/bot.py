import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from app.journal.config import JournalSettings
from app.journal.domain import SyncError
from app.journal.google import GoogleJournal
from app.journal.models import Account
from app.journal.service import JournalService, SyncWorker
from app.journal.store import Store

logger = logging.getLogger(__name__)


def keyboard(response):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"j|{response['nonce']}|{action}|{value}")
         for label, action, value in row] for row in response["buttons"]
    ])


def build_router(service: JournalService):
    router = Router(name="trade_journal")

    async def deliver(message, response):
        await message.answer(response["text"], reply_markup=keyboard(response), parse_mode=None)

    @router.callback_query()
    async def callback(query: CallbackQuery):
        if not isinstance(query.message, Message) or query.message.chat.type != "private":
            await query.answer("Используйте личный чат с ботом.")
            return
        await query.answer()
        parts = (query.data or "").split("|", 3)
        if len(parts) != 4 or parts[0] != "j":
            return
        response = await service.handle(query.from_user.id, "callback:" + query.id,
                                        nonce=parts[1], action=parts[2], value=parts[3])
        await deliver(query.message, response)

    @router.message()
    async def message(message: Message, bot: Bot):
        if not message.from_user or message.chat.type != "private":
            return
        user = message.from_user.id
        event = f"message:{message.chat.id}:{message.message_id}"
        image = None
        if service.settings.authorized(user) and (message.photo or message.document):
            file = message.photo[-1] if message.photo else message.document
            mime = "image/jpeg" if message.photo else (file.mime_type or "")
            extensions = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
            if mime not in extensions:
                await message.answer("Поддерживаются JPG, PNG и WebP.")
                return
            if file.file_size is None or file.file_size > service.settings.max_image_bytes:
                await message.answer("Изображение слишком большое или размер неизвестен. Максимум 15 МБ.")
                return
            downloaded = await bot.download(file.file_id)
            content = downloaded.read()
            if len(content) > service.settings.max_image_bytes:
                await message.answer("Изображение превышает допустимый размер.")
                return
            # Check actual magic bytes rather than trusting the document MIME alone.
            valid = ((mime == "image/jpeg" and content.startswith(b"\xff\xd8\xff"))
                     or (mime == "image/png" and content.startswith(b"\x89PNG\r\n\x1a\n"))
                     or (mime == "image/webp" and content.startswith(b"RIFF") and content[8:12] == b"WEBP"))
            if not valid:
                await message.answer("Содержимое файла не соответствует формату изображения.")
                return
            image = {"file_id": file.file_id, "filename": f"image.{extensions[mime]}",
                     "mime": mime, "content": content}
        response = await service.handle(user, event, text=message.text, image=image)
        await deliver(message, response)

    return router


async def sync_loop(service: JournalService):
    worker = SyncWorker(service)
    while True:
        try:
            if not await worker.once():
                await asyncio.sleep(service.settings.sync_interval)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep durable worker alive; never log secret-bearing errors
            # No exception bodies or tracebacks: HTTP client errors can contain secrets.
            logger.error("Journal sync iteration failed; queued operations remain durable.")
            await asyncio.sleep(service.settings.sync_interval)


async def run(settings: JournalSettings | None = None):
    settings = settings or JournalSettings()
    if not settings.bot_token.get_secret_value() or not settings.admin_ids:
        raise RuntimeError("Set JOURNAL_BOT_TOKEN and JOURNAL_ADMIN_IDS in .env.journal")
    store = Store(settings.database_url)
    google = GoogleJournal(settings)
    bot = Bot(settings.bot_token.get_secret_value())
    service = JournalService(store, settings, google)
    task = None
    try:
        try:
            accounts, slots = await google.preflight()
        except SyncError as exc:
            async with store.sessions() as session:
                cached_accounts = await session.scalar(select(func.count()).select_from(Account))
            if not exc.retryable or not cached_accounts:
                raise RuntimeError(f"Journal Google preflight failed: {exc}") from exc
            # A previously validated cache allows durable entry during a transient outage.
            # Every remote mutation still requires a successful fresh preflight.
            logger.warning("Google temporarily unavailable; using previously validated journal cache.")
        else:
            await store.bootstrap(accounts, slots)
        dispatcher = Dispatcher()
        dispatcher.include_router(build_router(service))
        task = asyncio.create_task(sync_loop(service))
        # Serial updates avoid races between album elements and the Done callback.
        # Multiple users are additionally serialized transactionally in the database.
        await dispatcher.start_polling(bot, handle_as_tasks=False, allowed_updates=["message", "callback_query"])
    finally:
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await bot.session.close()
        await google.close()
        await store.close()


def main():
    logging.basicConfig(level=logging.INFO)
    # HTTP and SDK debug logs must never expose Google/Telegram tokens or uploaded bytes.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        asyncio.run(run())
    except (RuntimeError, TelegramAPIError) as exc:
        if isinstance(exc, TelegramAPIError):
            raise SystemExit("Telegram connection failed. Check bot token and connectivity.") from None
        raise SystemExit(str(exc)) from None
