import html
import logging
from datetime import UTC, datetime

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.service import AdminPermissionService
from app.core.config import get_settings
from app.db.models import (
    Admin,
    AdminNotificationDelivery,
    AdminNotificationPreference,
)

logger = logging.getLogger(__name__)

NOTIFICATION_CATEGORIES = ("payments", "subscriptions", "refunds", "errors", "system")


class AdminNotificationService:
    def __init__(self, session: AsyncSession, bot: Bot | None) -> None:
        self.session = session
        self.bot = bot

    async def notify(
        self,
        category: str,
        event: str,
        text: str,
        deduplication_key: str,
        channel_id: int | None = None,
    ) -> int:
        if category not in NOTIFICATION_CATEGORIES:
            raise ValueError(f"Unknown notification category: {category}")
        settings = get_settings()
        notification_bot = self.bot
        owns_bot = False
        ops_bot_token = getattr(settings, "ops_bot_token", "")
        ops_chat_ids = getattr(settings, "ops_chat_ids", [])
        if ops_bot_token:
            try:
                notification_bot = Bot(ops_bot_token)
                owns_bot = True
            except Exception as exc:
                logger.warning("Ops bot initialization failed error=%s", type(exc).__name__)
                return 0
        if notification_bot is None:
            return 0
        recipients = (
            ops_chat_ids
            if ops_bot_token and ops_chat_ids
            else await self._recipients(category, channel_id)
        )
        sent = 0
        try:
            for telegram_id in recipients:
                key = f"{event}:{deduplication_key}"[:255]
                if await self._was_delivered(telegram_id, key):
                    continue
                try:
                    message_text = (
                        _format_ops_notification(category, event, text)
                        if ops_bot_token
                        else text
                    )
                    await notification_bot.send_message(
                        telegram_id,
                        message_text[:4000],
                        parse_mode="HTML" if ops_bot_token else None,
                        disable_notification=category == "system",
                    )
                except Exception as exc:
                    logger.warning(
                        "Admin notification failed category=%s event=%s error=%s",
                        category,
                        event,
                        type(exc).__name__,
                    )
                    continue
                try:
                    async with self.session.begin_nested():
                        self.session.add(
                            AdminNotificationDelivery(
                                telegram_id=telegram_id,
                                category=category,
                                deduplication_key=key,
                                sent_at=datetime.now(UTC).replace(tzinfo=None),
                            )
                        )
                        await self.session.flush()
                except (IntegrityError, SQLAlchemyError) as exc:
                    logger.warning(
                        "Admin notification delivery record failed category=%s event=%s error=%s",
                        category,
                        event,
                        type(exc).__name__,
                    )
                    continue
                sent += 1
        finally:
            if owns_bot:
                try:
                    await notification_bot.session.close()
                except Exception as exc:
                    logger.warning("Ops bot session close failed error=%s", type(exc).__name__)
        logger.info(
            "Admin notifications processed category=%s event=%s recipients=%s sent=%s",
            category,
            event,
            len(recipients),
            sent,
        )
        return sent

    async def set_preference(self, telegram_id: int, category: str, enabled: bool) -> None:
        if category not in NOTIFICATION_CATEGORIES:
            raise ValueError(f"Unknown notification category: {category}")
        preference = await self.session.scalar(
            select(AdminNotificationPreference).where(
                AdminNotificationPreference.telegram_id == telegram_id,
                AdminNotificationPreference.category == category,
            )
        )
        if preference is None:
            preference = AdminNotificationPreference(
                telegram_id=telegram_id,
                category=category,
            )
            self.session.add(preference)
        preference.enabled = enabled
        await self.session.flush()

    async def set_all_preferences(self, telegram_id: int, enabled: bool) -> None:
        for category in NOTIFICATION_CATEGORIES:
            await self.set_preference(telegram_id, category, enabled)

    async def preferences(self, telegram_id: int) -> dict[str, bool]:
        rows = list(
            await self.session.scalars(
                select(AdminNotificationPreference).where(
                    AdminNotificationPreference.telegram_id == telegram_id
                )
            )
        )
        values = {category: True for category in NOTIFICATION_CATEGORIES}
        values.update({row.category: row.enabled for row in rows})
        return values

    async def _recipients(self, category: str, channel_id: int | None) -> list[int]:
        db_ids = list(
            await self.session.scalars(
                select(Admin.telegram_id).where(Admin.is_active.is_(True))
            )
        )
        candidates = set(db_ids) | set(get_settings().admin_ids)
        permission = AdminPermissionService(self.session)
        recipients = []
        for telegram_id in candidates:
            if channel_id is not None and not await permission.can_manage_channel(
                telegram_id, channel_id
            ):
                continue
            preference = await self.session.scalar(
                select(AdminNotificationPreference.enabled).where(
                    AdminNotificationPreference.telegram_id == telegram_id,
                    AdminNotificationPreference.category == category,
                )
            )
            if preference is not False:
                recipients.append(telegram_id)
        return recipients

    async def _was_delivered(self, telegram_id: int, key: str) -> bool:
        return (
            await self.session.scalar(
                select(AdminNotificationDelivery.id).where(
                    AdminNotificationDelivery.telegram_id == telegram_id,
                    AdminNotificationDelivery.deduplication_key == key,
                )
            )
            is not None
        )


async def notify_runtime(bot: Bot | None, component: str, event: str, detail: str = "") -> None:
    from app.db.session import async_session_factory

    # Routine container restarts are visible in Docker/systemd; keep them out of the ops chat.
    if not detail and component.lower() in {"api", "bot", "scheduler"}:
        return
    try:
        async with async_session_factory() as session:
            text = f"Сервис {component}: {event}."
            if detail:
                text += f" {detail[:500]}"
            timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M")
            await AdminNotificationService(session, bot).notify(
                "system" if event in {"запущен", "остановлен"} else "errors",
                f"runtime.{event}",
                text,
                f"{component}:{event}:{timestamp}",
            )
            await session.commit()
    except Exception as exc:
        logger.warning(
            "Runtime notification failed component=%s event=%s error=%s",
            component,
            event,
            type(exc).__name__,
        )


def _format_ops_notification(category: str, event: str, text: str) -> str:
    now = datetime.now(UTC)
    titles = {
        "payments": "PROP GATE / PAYMENT",
        "subscriptions": "PROP GATE / ACCESS",
        "refunds": "PROP GATE / REFUND",
        "errors": "PROP GATE / ALERT",
        "system": "PROP GATE / SYSTEM",
    }
    lines = [titles.get(category, "PROP GATE / LOG"), "=" * 28]
    if event.startswith("runtime."):
        session_minute = (now.minute // 5) * 5
        session_id = now.replace(minute=session_minute, second=0, microsecond=0).strftime(
            "%Y%m%d-%H%M"
        )
        lines.extend(
            [
                f"STARTUP SESSION: {session_id}",
                f"COMPONENT: {_runtime_component(text)}",
            ]
        )
    lines.extend(
        [
            f"EVENT: {event}",
            f"TIME UTC: {now:%Y-%m-%d %H:%M:%S}",
            "-" * 28,
            text,
        ]
    )
    return "\n".join(lines)


def _runtime_component(text: str) -> str:
    prefix = "Сервис "
    if text.startswith(prefix):
        return text[len(prefix):].split(":", maxsplit=1)[0].upper()
    return "UNKNOWN"


def _format_ops_notification(category: str, event: str, text: str) -> str:
    """Render one compact, Telegram-safe operations message."""
    titles = {
        "payments": "PAYMENT",
        "subscriptions": "ACCESS",
        "refunds": "REFUND",
        "errors": "ALERT",
        "system": "SYSTEM",
    }
    now = datetime.now(UTC)
    return "\n".join(
        [
            f"<b>PROP GATE · {titles.get(category, 'LOG')}</b>",
            f"<code>{html.escape(event)}</code> · {now:%Y-%m-%d %H:%M:%S} UTC",
            html.escape(text),
        ]
    )
