import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.admin.settings_service import RuntimeSettingsService
from app.core.config import get_settings
from app.db.enums import PaymentStatus, SubscriptionStatus
from app.db.models import (
    Channel,
    InviteLink,
    NotificationLog,
    Payment,
    Plan,
    Subscription,
    User,
)
from app.services.admin_notification_service import AdminNotificationService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccessChangeResult:
    subscription: Subscription
    telegram_access_changed: bool
    error: str | None = None


class SubscriptionService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def activate_from_payment(
        self,
        payment: Payment,
        bot: Bot | None = None,
    ) -> tuple[Subscription, InviteLink, str | None]:
        await self.session.scalar(
            select(User.id).where(User.id == payment.user_id).with_for_update()
        )
        existing_for_payment = await self.session.scalar(
            select(Subscription)
            .options(
                selectinload(Subscription.invite_link),
                selectinload(Subscription.channel),
            )
            .where(Subscription.payment_id == payment.id)
            .with_for_update()
        )
        if existing_for_payment is not None:
            if existing_for_payment.status != SubscriptionStatus.ACTIVE:
                raise ValueError("Payment subscription has already been revoked")
            payment.status = PaymentStatus.PAID
            payment.paid_at = payment.paid_at or _utcnow()
            invite_link = await self._create_or_refresh_invite(
                existing_for_payment,
                existing_for_payment.channel,
                bot,
            )
            materials_invite = await self._create_materials_invite(existing_for_payment, bot)
            return existing_for_payment, invite_link, materials_invite

        plan = await self.session.get(Plan, payment.plan_id, options=[selectinload(Plan.channel)])
        if plan is None:
            raise ValueError("Payment plan not found")

        now = _utcnow()
        subscription = await self._active_subscription(payment.user_id, plan.channel_id)
        if subscription is None:
            subscription = existing_for_payment or Subscription(
                user_id=payment.user_id,
                channel_id=plan.channel_id,
                starts_at=now,
                expires_at=now,
            )
            self.session.add(subscription)

        base_expiry = max(now, subscription.expires_at)
        subscription.payment_id = payment.id
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.expires_at = base_expiry + timedelta(days=plan.duration_days)
        subscription.cancelled_at = None
        subscription.revoked_at = None
        subscription.revoke_reason = None
        subscription.source = "dev" if payment.provider == "dev" else "payment"
        payment.status = PaymentStatus.PAID
        payment.paid_at = payment.paid_at or now
        await self.session.flush()

        invite_link = await self._create_or_refresh_invite(subscription, plan.channel, bot)
        materials_invite = await self._create_materials_invite(subscription, bot)
        await self._notify_admins(
            bot,
            "subscriptions",
            "subscription.activated",
            f"Доступ выдан пользователю {payment.user_id} по платежу #{payment.id}.",
            f"payment:{payment.id}",
            plan.channel_id,
        )
        return subscription, invite_link, materials_invite

    async def grant_manual(
        self,
        telegram_id: int,
        channel_id: int,
        days: int,
        bot: Bot | None = None,
    ) -> tuple[Subscription, InviteLink]:
        if days <= 0:
            raise ValueError("Days must be greater than zero")

        channel = await self.session.get(Channel, channel_id)
        if channel is None:
            raise ValueError("Channel not found")

        user = await self.session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            user = User(telegram_id=telegram_id)
            self.session.add(user)
            await self.session.flush()
        await self.session.scalar(
            select(User.id).where(User.id == user.id).with_for_update()
        )

        now = _utcnow()
        subscription = await self._active_subscription(user.id, channel.id)
        if subscription is None:
            subscription = Subscription(
                user_id=user.id,
                channel_id=channel.id,
                payment_id=None,
                status=SubscriptionStatus.ACTIVE,
                starts_at=now,
                expires_at=now,
                source="manual",
            )
            self.session.add(subscription)

        subscription.status = SubscriptionStatus.ACTIVE
        subscription.expires_at = max(now, subscription.expires_at) + timedelta(days=days)
        subscription.cancelled_at = None
        subscription.revoked_at = None
        subscription.revoke_reason = None
        subscription.source = "manual"
        await self.session.flush()

        invite_link = await self._create_or_refresh_invite(subscription, channel, bot)
        await self._notify_admins(
            bot,
            "subscriptions",
            "subscription.granted",
            f"Вручную выдан доступ пользователю {telegram_id} на {days} дн.",
            f"manual:{subscription.id}:{subscription.expires_at.isoformat()}",
            channel.id,
        )
        return subscription, invite_link

    async def revoke(
        self,
        subscription_id: int,
        bot: Bot | None,
        reason: str = "manual revoke",
        status: SubscriptionStatus = SubscriptionStatus.CANCELLED,
    ) -> AccessChangeResult:
        subscription = await self.session.scalar(
            select(Subscription)
            .options(
                selectinload(Subscription.user),
                selectinload(Subscription.channel),
                selectinload(Subscription.invite_link),
            )
            .where(Subscription.id == subscription_id)
            .with_for_update()
        )
        if subscription is None:
            raise ValueError("Subscription not found")
        if subscription.status != SubscriptionStatus.ACTIVE:
            return AccessChangeResult(subscription, False)
        return await self._revoke_loaded(subscription, bot, reason, status)

    async def extend_manual(
        self,
        subscription_id: int,
        days: int,
        bot: Bot | None = None,
    ) -> tuple[Subscription, InviteLink]:
        if days <= 0:
            raise ValueError("Days must be greater than zero")
        subscription = await self.session.scalar(
            select(Subscription)
            .options(selectinload(Subscription.channel), selectinload(Subscription.invite_link))
            .where(Subscription.id == subscription_id)
            .with_for_update()
        )
        if subscription is None:
            raise ValueError("Subscription not found")
        now = _utcnow()
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.expires_at = max(now, subscription.expires_at) + timedelta(days=days)
        subscription.cancelled_at = None
        subscription.revoked_at = None
        subscription.revoke_reason = None
        subscription.source = "manual"
        await self.session.flush()
        invite = await self._create_or_refresh_invite(subscription, subscription.channel, bot)
        return subscription, invite

    async def refresh_invite(
        self,
        subscription_id: int,
        bot: Bot,
    ) -> tuple[Subscription, InviteLink]:
        subscription = await self.session.scalar(
            select(Subscription)
            .options(selectinload(Subscription.channel), selectinload(Subscription.invite_link))
            .where(
                Subscription.id == subscription_id,
                Subscription.status == SubscriptionStatus.ACTIVE,
            )
            .with_for_update()
        )
        if subscription is None:
            raise ValueError("Active subscription not found")
        invite = await self._create_or_refresh_invite(subscription, subscription.channel, bot)
        return subscription, invite

    async def revoke_user(
        self,
        telegram_id: int,
        channel_id: int,
        bot: Bot | None,
        reason: str = "manual revoke",
    ) -> AccessChangeResult:
        subscription = await self.session.scalar(
            select(Subscription)
            .join(Subscription.user)
            .options(
                selectinload(Subscription.user),
                selectinload(Subscription.channel),
                selectinload(Subscription.invite_link),
            )
            .where(
                User.telegram_id == telegram_id,
                Subscription.channel_id == channel_id,
                Subscription.status == SubscriptionStatus.ACTIVE,
            )
            .order_by(Subscription.expires_at.desc())
            .with_for_update()
        )
        if subscription is None:
            raise ValueError("Active subscription not found")
        return await self._revoke_loaded(
            subscription,
            bot,
            reason,
            SubscriptionStatus.CANCELLED,
        )

    async def expire_due_subscriptions(self, bot: Bot | None = None) -> int:
        now = _utcnow()
        result = await self.session.scalars(
            select(Subscription)
            .options(
                selectinload(Subscription.user),
                selectinload(Subscription.channel),
                selectinload(Subscription.invite_link),
            )
            .where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.expires_at <= now,
            )
            .with_for_update(skip_locked=True)
        )
        subscriptions = list(result)
        for subscription in subscriptions:
            await self._revoke_loaded(
                subscription,
                bot,
                "subscription expired",
                SubscriptionStatus.EXPIRED,
            )
            await self._notify_expired(subscription, bot)
        await self.session.commit()
        return len(subscriptions)

    async def send_expiration_reminders(
        self,
        bot: Bot | None,
        days_before: list[int],
    ) -> int:
        if bot is None:
            return 0

        now = _utcnow()
        max_days = max(days_before, default=0)
        result = await self.session.scalars(
            select(Subscription)
            .options(selectinload(Subscription.user), selectinload(Subscription.channel))
            .where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.expires_at > now,
                Subscription.expires_at <= now + timedelta(days=max_days),
            )
            .with_for_update(skip_locked=True)
        )
        sent = 0
        for subscription in result:
            remaining = subscription.expires_at - now
            for days in sorted(days_before, reverse=True):
                notification_type = f"expires_{days}d"
                if remaining > timedelta(days=days):
                    continue
                if await self._notification_exists(subscription.id, notification_type):
                    continue
                try:
                    await bot.send_message(
                        subscription.user.telegram_id,
                        (
                            f"Подписка на «{subscription.channel.title}» закончится через "
                            f"{days} дн. Продлите доступ заранее."
                        ),
                    )
                except TelegramAPIError as exc:
                    logger.warning(
                        "Subscription reminder failed subscription_id=%s error=%s",
                        subscription.id,
                        type(exc).__name__,
                    )
                    continue
                self.session.add(
                    NotificationLog(
                        user_id=subscription.user_id,
                        subscription_id=subscription.id,
                        type=notification_type,
                        sent_at=now,
                    )
                )
                sent += 1
        await self.session.commit()
        return sent

    async def _active_subscription(self, user_id: int, channel_id: int) -> Subscription | None:
        return await self.session.scalar(
            select(Subscription)
            .options(selectinload(Subscription.invite_link))
            .where(
                Subscription.user_id == user_id,
                Subscription.channel_id == channel_id,
                Subscription.status == SubscriptionStatus.ACTIVE,
            )
            .order_by(Subscription.expires_at.desc())
            .with_for_update()
        )

    async def _create_or_refresh_invite(
        self,
        subscription: Subscription,
        channel: Channel,
        bot: Bot | None,
    ) -> InviteLink:
        now = _utcnow()
        existing = await self.session.scalar(
            select(InviteLink).where(InviteLink.subscription_id == subscription.id)
        )
        if (
            existing is not None
            and existing.link
            and not existing.is_revoked
            and existing.expire_at is not None
            and existing.expire_at > now
        ):
            return existing
        ttl_minutes = await RuntimeSettingsService(self.session).invite_ttl_minutes()
        expire_at = now + timedelta(minutes=ttl_minutes)
        provider_id: str | None = None
        link_value = "tg://join?invite=replace-with-created-link"
        if bot is not None:
            provider_id = f"sub-{subscription.id}"
            try:
                invite = await bot.create_chat_invite_link(
                    chat_id=channel.telegram_chat_id,
                    name=provider_id,
                    expire_date=expire_at,
                    member_limit=1,
                )
            except TelegramAPIError as exc:
                await self._notify_admins(
                    bot,
                    "errors",
                    "invite.create_failed",
                    f"Не удалось создать invite-ссылку для подписки #{subscription.id}: {type(exc).__name__}.",
                    f"subscription:{subscription.id}",
                    channel.id,
                )
                raise
            link_value = invite.invite_link

        invite_link = existing
        if invite_link is None:
            invite_link = InviteLink(subscription_id=subscription.id, link=link_value)
            self.session.add(invite_link)
        subscription.invite_link = invite_link
        invite_link.link = link_value
        invite_link.expire_at = expire_at
        invite_link.used_at = None
        invite_link.is_revoked = False
        invite_link.revoked_at = None
        invite_link.provider_invite_link_id = provider_id
        await self.session.flush()
        return invite_link

    async def _create_materials_invite(
        self,
        subscription: Subscription,
        bot: Bot | None,
    ) -> str | None:
        channel_id = await RuntimeSettingsService(self.session).get(
            RuntimeSettingsService.MATERIALS_CHANNEL_ID,
            get_settings().materials_channel_id,
        )
        if bot is None or channel_id is None:
            return None
        invite_link = subscription.invite_link
        now = _utcnow()
        if (
            invite_link is not None
            and invite_link.materials_link
            and not invite_link.materials_is_revoked
            and invite_link.materials_expire_at
            and invite_link.materials_expire_at > now
        ):
            return invite_link.materials_link
        ttl_minutes = await RuntimeSettingsService(self.session).invite_ttl_minutes()
        expire_at = now + timedelta(minutes=ttl_minutes)
        try:
            invite = await bot.create_chat_invite_link(
                chat_id=channel_id,
                name=f"materials-sub-{subscription.id}",
                expire_date=expire_at,
                member_limit=1,
            )
        except TelegramAPIError as exc:
            logger.warning(
                "Materials invite creation failed subscription_id=%s channel_id=%s error=%s",
                subscription.id,
                channel_id,
                type(exc).__name__,
            )
            await self._notify_admins(
                bot,
                "errors",
                "materials.invite_failed",
                f"Не удалось создать ссылку на материалы для подписки #{subscription.id}.",
                f"subscription:{subscription.id}",
                subscription.channel_id,
            )
            return None
        if invite_link is None:
            raise RuntimeError("Primary invite link is missing")
        invite_link.materials_channel_id = channel_id
        invite_link.materials_link = invite.invite_link
        invite_link.materials_expire_at = expire_at
        invite_link.materials_is_revoked = False
        invite_link.materials_revoked_at = None
        await self.session.flush()
        return invite.invite_link

    async def _revoke_loaded(
        self,
        subscription: Subscription,
        bot: Bot | None,
        reason: str,
        status: SubscriptionStatus,
    ) -> AccessChangeResult:
        now = _utcnow()
        changed = False
        error: str | None = None
        if bot is not None:
            try:
                await bot.ban_chat_member(
                    chat_id=subscription.channel.telegram_chat_id,
                    user_id=subscription.user.telegram_id,
                )
                await bot.unban_chat_member(
                    chat_id=subscription.channel.telegram_chat_id,
                    user_id=subscription.user.telegram_id,
                    only_if_banned=True,
                )
                changed = True
            except TelegramAPIError as exc:
                error = type(exc).__name__
                logger.warning(
                    "Telegram access revoke failed subscription_id=%s channel_id=%s error=%s",
                    subscription.id,
                    subscription.channel_id,
                    error,
                )

            if subscription.invite_link and not subscription.invite_link.is_revoked:
                try:
                    await bot.revoke_chat_invite_link(
                        chat_id=subscription.channel.telegram_chat_id,
                        invite_link=subscription.invite_link.link,
                    )
                except TelegramAPIError as exc:
                    logger.warning(
                        "Invite revoke failed subscription_id=%s error=%s",
                        subscription.id,
                        type(exc).__name__,
                    )

            materials_channel_id = (
                subscription.invite_link.materials_channel_id
                if subscription.invite_link and subscription.invite_link.materials_channel_id
                else await RuntimeSettingsService(self.session).get(
                    RuntimeSettingsService.MATERIALS_CHANNEL_ID,
                    get_settings().materials_channel_id,
                )
            )
            if (
                materials_channel_id is not None
                and subscription.invite_link
                and subscription.invite_link.materials_link
                and not subscription.invite_link.materials_is_revoked
            ):
                try:
                    await bot.revoke_chat_invite_link(
                        chat_id=materials_channel_id,
                        invite_link=subscription.invite_link.materials_link,
                    )
                except TelegramAPIError as exc:
                    logger.warning(
                        "Materials invite revoke failed subscription_id=%s error=%s",
                        subscription.id,
                        type(exc).__name__,
                    )
            if materials_channel_id is not None and subscription.payment_id is not None:
                try:
                    await bot.ban_chat_member(
                        chat_id=materials_channel_id,
                        user_id=subscription.user.telegram_id,
                    )
                    await bot.unban_chat_member(
                        chat_id=materials_channel_id,
                        user_id=subscription.user.telegram_id,
                        only_if_banned=True,
                    )
                except TelegramAPIError as exc:
                    logger.warning(
                        "Materials access revoke failed subscription_id=%s channel_id=%s error=%s",
                        subscription.id,
                        materials_channel_id,
                        type(exc).__name__,
                    )

        subscription.status = status
        subscription.revoked_at = now
        subscription.revoke_reason = reason[:512]
        if status == SubscriptionStatus.CANCELLED:
            subscription.cancelled_at = now
        if subscription.invite_link:
            subscription.invite_link.is_revoked = True
            subscription.invite_link.revoked_at = now
            if subscription.invite_link.materials_link:
                subscription.invite_link.materials_is_revoked = True
                subscription.invite_link.materials_revoked_at = now
        await self.session.flush()
        event = "subscription.expired" if status == SubscriptionStatus.EXPIRED else "subscription.revoked"
        await self._notify_admins(
            bot,
            "subscriptions",
            event,
            f"Доступ пользователя {subscription.user.telegram_id} отозван: {reason[:120]}.",
            f"{subscription.id}:{status.value}",
            subscription.channel_id,
        )
        if error:
            await self._notify_admins(
                bot,
                "errors",
                "subscription.telegram_revoke_failed",
                f"Подписка #{subscription.id} отменена в базе, но Telegram вернул {error}.",
                f"{subscription.id}:{error}",
                subscription.channel_id,
            )
        return AccessChangeResult(subscription, changed, error)

    async def _notify_expired(self, subscription: Subscription, bot: Bot | None) -> None:
        if bot is None or await self._notification_exists(subscription.id, "expired"):
            return
        try:
            await bot.send_message(
                subscription.user.telegram_id,
                (
                    f"Подписка на «{subscription.channel.title}» закончилась. "
                    "Доступ к каналу закрыт."
                ),
            )
        except TelegramAPIError as exc:
            logger.warning(
                "Expiration notification failed subscription_id=%s error=%s",
                subscription.id,
                type(exc).__name__,
            )
            return
        self.session.add(
            NotificationLog(
                user_id=subscription.user_id,
                subscription_id=subscription.id,
                type="expired",
                sent_at=_utcnow(),
            )
        )

    async def _notification_exists(self, subscription_id: int, notification_type: str) -> bool:
        return (
            await self.session.scalar(
                select(NotificationLog.id).where(
                    NotificationLog.subscription_id == subscription_id,
                    NotificationLog.type == notification_type,
                )
            )
            is not None
        )

    async def _notify_admins(
        self,
        bot: Bot | None,
        category: str,
        event: str,
        text: str,
        key: str,
        channel_id: int,
    ) -> None:
        await AdminNotificationService(self.session, bot).notify(
            category,
            event,
            text,
            key,
            channel_id,
        )


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
