import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
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
    PaymentApplication,
    Plan,
    Subscription,
    User,
)
from app.services.admin_notification_service import AdminNotificationService
from app.services.promo_service import PromoService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccessChangeResult:
    subscription: Subscription
    telegram_access_changed: bool
    error: str | None = None


class ChannelAccessError(ValueError):
    pass


class SubscriptionService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def activate_from_payment(
        self,
        payment: Payment,
        bot: Bot | None = None,
    ) -> tuple[Subscription, InviteLink, str | None]:
        if payment.status == PaymentStatus.REFUNDED:
            raise ValueError("Refunded payment cannot grant access")
        payment = await self.session.scalar(
            select(Payment)
            .options(selectinload(Payment.user), selectinload(Payment.plan))
            .where(Payment.id == payment.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if payment is None or payment.status == PaymentStatus.REFUNDED:
            raise ValueError("Refunded or missing payment cannot grant access")
        await self.session.scalar(
            select(User.id).where(User.id == payment.user_id).with_for_update()
        )
        application = await self.session.scalar(
            select(PaymentApplication)
            .where(PaymentApplication.payment_id == payment.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        existing_for_payment = await self.session.scalar(
            select(Subscription)
            .options(
                selectinload(Subscription.invite_link),
                selectinload(Subscription.channel),
            )
            .where(
                Subscription.id == application.subscription_id
                if application is not None
                else Subscription.payment_id == payment.id
            )
            .with_for_update()
        )
        if existing_for_payment is not None:
            if (
                application is not None
                and application.legacy
                and existing_for_payment.payment_id != payment.id
            ):
                raise ValueError("Historical payment application requires manual reconciliation")
            if application is not None and application.reversed_at is not None:
                raise ValueError("Payment has already been refunded")
            if (
                existing_for_payment.status != SubscriptionStatus.ACTIVE
                or existing_for_payment.expires_at <= _utcnow()
            ):
                raise ValueError("Payment subscription has already been revoked")
            payment.status = PaymentStatus.PAID
            payment.paid_at = payment.paid_at or _utcnow()
            invite_link, materials_invite = await self._provision_access_links(
                existing_for_payment,
                existing_for_payment.channel,
                bot,
            )
            await PromoService(self.session).finalize_redemption(
                payment,
                existing_for_payment,
            )
            return existing_for_payment, invite_link, materials_invite

        plan = await self.session.scalar(
            select(Plan).options(selectinload(Plan.channel)).where(Plan.id == payment.plan_id)
        )
        if plan is None:
            raise ValueError("Payment plan not found")

        if payment.metadata_json.get("terms_origin") == "migration_catalog_unverified":
            raise ValueError("Historical purchase terms require manual reconciliation")
        now = _utcnow()
        channel_id = payment.purchased_channel_id or plan.channel_id
        duration_days = payment.purchased_duration_days or plan.duration_days
        channel = await self.session.get(Channel, channel_id)
        if channel is None or duration_days <= 0:
            raise ValueError("Purchased access terms are invalid")
        subscription = await self._active_subscription(payment.user_id, channel_id)
        if subscription is None:
            subscription = existing_for_payment or Subscription(
                user_id=payment.user_id,
                channel_id=channel_id,
                starts_at=now,
                expires_at=now,
            )
            self.session.add(subscription)

        base_expiry = max(now, subscription.expires_at)
        subscription.payment_id = payment.id
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.period_started_at = base_expiry
        subscription.expires_at = base_expiry + timedelta(days=duration_days)
        subscription.cancelled_at = None
        subscription.revoked_at = None
        subscription.revoke_reason = None
        subscription.source = "dev" if payment.provider == "dev" else "payment"
        payment.status = PaymentStatus.PAID
        payment.paid_at = payment.paid_at or now
        await self.session.flush()
        self.session.add(
            PaymentApplication(
                payment_id=payment.id,
                subscription_id=subscription.id,
                days_granted=duration_days,
                applied_at=now,
                starts_at=base_expiry,
                expires_at=subscription.expires_at,
            )
        )
        await self.session.flush()

        invite_link, materials_invite = await self._provision_access_links(
            subscription,
            channel,
            bot,
            force_new=True,
        )
        await PromoService(self.session).finalize_redemption(
            payment,
            subscription,
        )
        await self._notify_admins(
            bot,
            "subscriptions",
            "subscription.activated",
            f"Доступ выдан пользователю {payment.user_id} по платежу #{payment.id}.",
            f"payment:{payment.id}",
            channel_id,
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
        await self.session.scalar(select(User.id).where(User.id == user.id).with_for_update())

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

        base_expiry = max(now, subscription.expires_at)
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.period_started_at = base_expiry
        subscription.expires_at = base_expiry + timedelta(days=days)
        subscription.cancelled_at = None
        subscription.revoked_at = None
        subscription.revoke_reason = None
        subscription.source = "manual"
        await self.session.flush()

        invite_link, _ = await self._provision_access_links(
            subscription,
            channel,
            bot,
            force_new=True,
        )
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
        )
        if subscription is None:
            raise ValueError("Subscription not found")
        await self.session.scalar(
            select(User.id).where(User.id == subscription.user_id).with_for_update()
        )
        subscription = await self.session.scalar(
            select(Subscription)
            .options(
                selectinload(Subscription.user),
                selectinload(Subscription.channel),
                selectinload(Subscription.invite_link),
            )
            .where(Subscription.id == subscription_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
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
        user_id = await self.session.scalar(
            select(Subscription.user_id).where(Subscription.id == subscription_id)
        )
        if user_id is not None:
            await self.session.scalar(select(User.id).where(User.id == user_id).with_for_update())
        subscription = await self.session.scalar(
            select(Subscription)
            .options(selectinload(Subscription.channel), selectinload(Subscription.invite_link))
            .where(Subscription.id == subscription_id)
            .with_for_update()
        )
        if subscription is None:
            raise ValueError("Subscription not found")
        now = _utcnow()
        base_expiry = max(now, subscription.expires_at)
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.period_started_at = base_expiry
        subscription.expires_at = base_expiry + timedelta(days=days)
        subscription.cancelled_at = None
        subscription.revoked_at = None
        subscription.revoke_reason = None
        subscription.source = "manual"
        await self.session.flush()
        invite, _ = await self._provision_access_links(
            subscription,
            subscription.channel,
            bot,
            force_new=True,
        )
        return subscription, invite

    async def refresh_invite(
        self,
        subscription_id: int,
        bot: Bot,
    ) -> tuple[Subscription, InviteLink]:
        now = _utcnow()
        subscription = await self.session.scalar(
            select(Subscription)
            .options(selectinload(Subscription.channel), selectinload(Subscription.invite_link))
            .where(
                Subscription.id == subscription_id,
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.expires_at > now,
            )
            .with_for_update()
        )
        if subscription is None:
            raise ValueError("Active subscription not found")
        existing_invite = subscription.invite_link
        materials_channel_id = await RuntimeSettingsService(self.session).get(
            RuntimeSettingsService.MATERIALS_CHANNEL_ID,
            get_settings().materials_channel_id,
        )
        join_request_ready = (
            existing_invite is not None
            and existing_invite.primary_join_request_enabled
            and (
                materials_channel_id is None
                or (
                    existing_invite.materials_channel_id == materials_channel_id
                    and bool(existing_invite.materials_link)
                    and existing_invite.materials_join_request_enabled
                )
            )
        )
        if join_request_ready and existing_invite is not None:
            last_refresh = existing_invite.updated_at or existing_invite.created_at
            if last_refresh and now - last_refresh < timedelta(minutes=5):
                raise ValueError(
                    "Новые ссылки уже выдавались недавно. Попробуйте через несколько минут."
                )
        invite, _ = await self._provision_access_links(
            subscription,
            subscription.channel,
            bot,
            force_new=True,
        )
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
            .execution_options(populate_existing=True)
        )
        if subscription is None:
            raise ValueError("Active subscription not found")
        return await self._revoke_loaded(
            subscription,
            bot,
            reason,
            SubscriptionStatus.CANCELLED,
        )

    async def expire_due_subscriptions(
        self, bot: Bot | None = None, *, defer_telegram: bool = False
    ) -> int:
        from app.services.access_operation_service import AccessOperationService

        now = _utcnow()
        result = await self.session.scalars(
            select(Subscription)
            .join(User, User.id == Subscription.user_id)
            .options(
                selectinload(Subscription.user),
                selectinload(Subscription.channel),
                selectinload(Subscription.invite_link),
            )
            .where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.expires_at <= now,
            )
            .order_by(Subscription.user_id, Subscription.id)
            .with_for_update(of=User, skip_locked=True)
            .limit(100)
        )
        subscriptions = list(result)
        expired = 0
        for subscription in subscriptions:
            user_id = await self.session.scalar(
                select(User.id)
                .where(User.id == subscription.user_id)
                .with_for_update(skip_locked=True)
            )
            if user_id is None:
                continue
            subscription = await self.session.scalar(
                select(Subscription)
                .options(
                    selectinload(Subscription.user),
                    selectinload(Subscription.channel),
                    selectinload(Subscription.invite_link),
                )
                .where(Subscription.id == subscription.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if subscription.status != SubscriptionStatus.ACTIVE or subscription.expires_at > now:
                continue
            await self._revoke_loaded(
                subscription,
                None if defer_telegram else bot,
                "subscription expired",
                SubscriptionStatus.EXPIRED,
            )
            if defer_telegram or bot is None:
                await AccessOperationService(self.session).enqueue(
                    f"expired-notice:{subscription.id}:{subscription.expires_at.isoformat()}",
                    "expiry_notice",
                    subscription_id=subscription.id,
                    telegram_id=subscription.user.telegram_id,
                    chat_id=subscription.channel.telegram_chat_id,
                    expires_at_snapshot=subscription.expires_at,
                )
            else:
                try:
                    await self._notify_expired(subscription, bot)
                except TelegramAPIError as exc:
                    operation = await AccessOperationService(self.session).enqueue(
                        f"expired-notice:{subscription.id}:{subscription.expires_at.isoformat()}",
                        "expiry_notice",
                        subscription_id=subscription.id,
                        telegram_id=subscription.user.telegram_id,
                        chat_id=subscription.channel.telegram_chat_id,
                        expires_at_snapshot=subscription.expires_at,
                    )
                    operation.last_error = type(exc).__name__
                    operation.next_attempt_at = _utcnow() + timedelta(
                        seconds=getattr(exc, "retry_after", 60)
                    )
            expired += 1
        await self.session.commit()
        return expired

    async def send_expiration_reminders(
        self,
        bot: Bot | None,
        intervals_minutes: list[int],
        *,
        time_budget_seconds: float = 30,
    ) -> int:
        if bot is None:
            return 0

        now = _utcnow()
        deadline = asyncio.get_running_loop().time() + time_budget_seconds
        normalized_intervals = sorted(
            {int(minutes) for minutes in intervals_minutes if 0 < int(minutes) <= 525_600},
            reverse=True,
        )
        max_minutes = max(normalized_intervals, default=0)
        if max_minutes == 0:
            return 0
        result = await self.session.scalars(
            select(Subscription)
            .options(selectinload(Subscription.user), selectinload(Subscription.channel))
            .where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.expires_at > now,
                Subscription.expires_at <= now + timedelta(minutes=max_minutes),
            )
            .with_for_update(skip_locked=True)
            .limit(100)
        )
        sent = 0
        for subscription in result:
            budget = deadline - asyncio.get_running_loop().time()
            if budget <= 0:
                break
            remaining = subscription.expires_at - now
            period_duration = subscription.expires_at - subscription.period_started_at
            candidates = [
                minutes
                for minutes in normalized_intervals
                if timedelta(minutes=minutes) < period_duration
                and remaining <= timedelta(minutes=minutes)
            ]
            if not candidates:
                continue
            threshold_minutes = min(candidates)
            if await self._notification_exists(
                subscription.id,
                subscription.expires_at,
                threshold_minutes,
            ):
                continue
            try:
                async with asyncio.timeout(min(10, budget)):
                    await bot.send_message(
                        subscription.user.telegram_id,
                        f"Подписка на «{subscription.channel.title}» закончится {subscription.expires_at:%d.%m.%Y %H:%M} UTC. Продлите доступ заранее.",
                    )
            except (TelegramAPIError, TimeoutError) as exc:
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
                    type=f"expires_{threshold_minutes}m",
                    expires_at_snapshot=subscription.expires_at,
                    threshold_minutes=threshold_minutes,
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

    async def _provision_access_links(
        self,
        subscription: Subscription,
        channel: Channel,
        bot: Bot | None,
        force_new: bool = False,
    ) -> tuple[InviteLink, str | None]:
        existing = await self.session.scalar(
            select(InviteLink).where(InviteLink.subscription_id == subscription.id)
        )
        previous_primary_link = (
            existing.link
            if existing is not None and existing.link and not existing.is_revoked
            else None
        )
        previous_materials_link = (
            existing.materials_link
            if (
                existing is not None
                and existing.materials_link
                and not existing.materials_is_revoked
            )
            else None
        )
        previous_materials_channel_id = (
            existing.materials_channel_id if existing is not None else None
        )
        if bot is not None:
            try:
                await self._ensure_bot_channel_permissions(
                    bot,
                    channel.telegram_chat_id,
                )
                materials_channel_id = await RuntimeSettingsService(self.session).get(
                    RuntimeSettingsService.MATERIALS_CHANNEL_ID,
                    get_settings().materials_channel_id,
                )
                if materials_channel_id is not None:
                    await self._ensure_bot_channel_permissions(
                        bot,
                        int(materials_channel_id),
                    )
            except ChannelAccessError as exc:
                logger.exception(
                    "event=invite.creation_failed kind=preflight "
                    "subscription_id=%s channel_id=%s error=%s",
                    subscription.id,
                    channel.telegram_chat_id,
                    type(exc).__name__,
                )
                await self._notify_admins(
                    bot,
                    "errors",
                    "invite.creation_failed",
                    f"Не пройдена проверка прав каналов для подписки #{subscription.id}.",
                    f"preflight:{subscription.id}",
                    channel.id,
                )
                raise
        invite = await self._create_or_refresh_invite(
            subscription,
            channel,
            bot,
            force_new=force_new,
        )
        try:
            materials = await self._create_materials_invite(
                subscription,
                bot,
                force_new=force_new,
            )
        except (TelegramAPIError, ChannelAccessError) as exc:
            if bot is not None and invite.link and invite.link != previous_primary_link:
                compensated = await self._revoke_invite_safely(
                    bot,
                    channel.telegram_chat_id,
                    invite.link,
                    subscription.id,
                    "primary_compensation",
                )
                if not compensated:
                    exc.cleanup_invites = [(channel.telegram_chat_id, invite.link)]
            raise
        if bot is not None and previous_primary_link and previous_primary_link != invite.link:
            await self._revoke_invite_safely(
                bot,
                channel.telegram_chat_id,
                previous_primary_link,
                subscription.id,
                "primary",
            )
        if (
            bot is not None
            and previous_materials_channel_id is not None
            and previous_materials_link
            and materials is not None
            and previous_materials_link != materials
        ):
            await self._revoke_invite_safely(
                bot,
                previous_materials_channel_id,
                previous_materials_link,
                subscription.id,
                "materials",
            )
        return invite, materials

    async def _create_or_refresh_invite(
        self,
        subscription: Subscription,
        channel: Channel,
        bot: Bot | None,
        force_new: bool = False,
    ) -> InviteLink:
        now = _utcnow()
        existing = await self.session.scalar(
            select(InviteLink).where(InviteLink.subscription_id == subscription.id)
        )
        if not force_new and (
            existing is not None
            and existing.link
            and not existing.is_revoked
            and existing.primary_join_request_enabled
            and existing.expire_at is not None
            and existing.expire_at > now
        ):
            return existing
        ttl_minutes = await RuntimeSettingsService(self.session).invite_ttl_minutes()
        expire_at = _invite_expiration(now, subscription.expires_at, ttl_minutes)
        provider_id: str | None = None
        link_value = "tg://join?invite=replace-with-created-link"
        if bot is not None:
            provider_id = f"sub-{subscription.id}"
            try:
                await self._ensure_bot_channel_permissions(bot, channel.telegram_chat_id)
                invite = await bot.create_chat_invite_link(
                    chat_id=channel.telegram_chat_id,
                    name=provider_id,
                    expire_date=expire_at,
                    creates_join_request=True,
                )
            except (TelegramAPIError, ChannelAccessError) as exc:
                logger.exception(
                    "event=invite.creation_failed kind=primary subscription_id=%s "
                    "channel_id=%s error=%s",
                    subscription.id,
                    channel.telegram_chat_id,
                    type(exc).__name__,
                )
                await self._notify_admins(
                    bot,
                    "errors",
                    "invite.creation_failed",
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
        invite_link.primary_join_request_enabled = bot is not None
        invite_link.revoked_at = None
        invite_link.provider_invite_link_id = provider_id
        await self.session.flush()
        logger.info(
            "event=invite.primary_created subscription_id=%s channel_id=%s",
            subscription.id,
            channel.telegram_chat_id,
        )
        return invite_link

    async def _create_materials_invite(
        self,
        subscription: Subscription,
        bot: Bot | None,
        force_new: bool = False,
    ) -> str | None:
        channel_id = await RuntimeSettingsService(self.session).get(
            RuntimeSettingsService.MATERIALS_CHANNEL_ID,
            get_settings().materials_channel_id,
        )
        if bot is None or channel_id is None:
            return None
        invite_link = subscription.invite_link
        now = _utcnow()
        if not force_new and (
            invite_link is not None
            and invite_link.materials_link
            and not invite_link.materials_is_revoked
            and invite_link.materials_join_request_enabled
            and invite_link.materials_channel_id == channel_id
            and invite_link.materials_expire_at
            and invite_link.materials_expire_at > now
        ):
            return invite_link.materials_link
        ttl_minutes = await RuntimeSettingsService(self.session).invite_ttl_minutes()
        expire_at = _invite_expiration(now, subscription.expires_at, ttl_minutes)
        try:
            await self._ensure_bot_channel_permissions(bot, channel_id)
            invite = await bot.create_chat_invite_link(
                chat_id=channel_id,
                name=f"materials-sub-{subscription.id}",
                expire_date=expire_at,
                creates_join_request=True,
            )
        except (TelegramAPIError, ChannelAccessError) as exc:
            logger.exception(
                "event=invite.creation_failed kind=materials subscription_id=%s "
                "channel_id=%s error=%s",
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
            if (
                invite_link is not None
                and invite_link.materials_join_request_enabled
                and invite_link.materials_channel_id == channel_id
                and invite_link.materials_link
                and not invite_link.materials_is_revoked
                and invite_link.materials_expire_at
                and invite_link.materials_expire_at > now
            ):
                return invite_link.materials_link
            raise
        if invite_link is None:
            raise RuntimeError("Primary invite link is missing")
        invite_link.materials_channel_id = channel_id
        invite_link.materials_link = invite.invite_link
        invite_link.materials_expire_at = expire_at
        invite_link.materials_is_revoked = False
        invite_link.materials_join_request_enabled = True
        invite_link.materials_revoked_at = None
        await self.session.flush()
        logger.info(
            "event=invite.materials_created subscription_id=%s channel_id=%s",
            subscription.id,
            channel_id,
        )
        return invite.invite_link

    @staticmethod
    async def _ensure_bot_channel_permissions(bot: Bot, chat_id: int) -> None:
        try:
            bot_user = await bot.get_me()
            member = await bot.get_chat_member(chat_id, bot_user.id)
        except TelegramAPIError as exc:
            raise ChannelAccessError(f"Bot cannot inspect channel {chat_id}") from exc
        if member.status == ChatMemberStatus.CREATOR:
            return
        if member.status != ChatMemberStatus.ADMINISTRATOR:
            raise ChannelAccessError(f"Bot is not an administrator of channel {chat_id}")
        if not getattr(member, "can_invite_users", False):
            raise ChannelAccessError(f"Bot cannot invite users to channel {chat_id}")
        if not getattr(member, "can_restrict_members", False):
            raise ChannelAccessError(f"Bot cannot restrict members in channel {chat_id}")

    async def _revoke_invite_safely(
        self,
        bot: Bot,
        chat_id: int,
        link: str,
        subscription_id: int,
        kind: str,
    ) -> bool:
        try:
            await bot.revoke_chat_invite_link(chat_id=chat_id, invite_link=link)
            return True
        except TelegramAPIError as exc:
            from app.services.access_operation_service import AccessOperationService

            exc.cleanup_invites = [(chat_id, link)]
            await AccessOperationService(self.session).enqueue_cleanup(exc)
            logger.info(
                "Old invite revoke skipped subscription_id=%s kind=%s error=%s",
                subscription_id,
                kind,
                type(exc).__name__,
            )
            return False

    async def _revoke_loaded(
        self,
        subscription: Subscription,
        bot: Bot | None,
        reason: str,
        status: SubscriptionStatus,
    ) -> AccessChangeResult:
        from app.services.access_operation_service import AccessOperationService

        now = _utcnow()
        service = AccessOperationService(self.session)
        subscription.status = status
        subscription.revoked_at = now
        subscription.revoke_reason = reason[:512]
        if status == SubscriptionStatus.CANCELLED:
            subscription.cancelled_at = now
        await self.session.flush()
        operations = []
        base = f"revoke:{subscription.id}:{subscription.expires_at.isoformat()}"
        values = {
            "subscription_id": subscription.id,
            "telegram_id": subscription.user.telegram_id,
            "expires_at_snapshot": subscription.expires_at,
        }
        chats = [(subscription.channel.telegram_chat_id, None)]
        invite = subscription.invite_link
        materials_channel_id = (
            invite.materials_channel_id
            if invite and invite.materials_channel_id
            else await RuntimeSettingsService(self.session).get(
                RuntimeSettingsService.MATERIALS_CHANNEL_ID, get_settings().materials_channel_id
            )
        )
        if materials_channel_id is not None:
            chats.append((materials_channel_id, "materials"))
        for chat_id, kind in chats:
            operation = await service.enqueue(
                f"{base}:member:{chat_id}", "remove_member", chat_id=chat_id, **values
            )
            operations.append(operation)
            link = invite.materials_link if invite and kind else invite.link if invite else None
            if link:
                operations.append(
                    await service.enqueue(
                        f"{base}:link:{chat_id}",
                        "revoke_link",
                        chat_id=chat_id,
                        invite_link=link,
                        **values,
                    )
                )
        failures = []
        for operation in operations:
            if not await service.execute(operation, bot):
                failures.append(operation.last_error or "pending")
        if invite:
            main_done = all(
                o.completed_at
                for o in operations
                if o.kind == "revoke_link" and o.chat_id == subscription.channel.telegram_chat_id
            )
            materials_done = all(
                o.completed_at
                for o in operations
                if o.kind == "revoke_link" and o.chat_id == materials_channel_id
            )
            invite.is_revoked = main_done
            invite.revoked_at = now if main_done else None
            if invite.materials_link:
                invite.materials_is_revoked = materials_done
                invite.materials_revoked_at = now if materials_done else None
        await self.session.flush()
        await self._notify_admins(
            bot,
            "subscriptions",
            "subscription.revoked",
            f"Подписка пользователя {subscription.user.telegram_id} завершена; Telegram: {'повтор запланирован' if failures else 'операции подтверждены'}.",
            f"{subscription.id}:{status.value}",
            subscription.channel_id,
        )
        return AccessChangeResult(
            subscription, not failures and bot is not None, ",".join(failures) if failures else None
        )

    async def _notify_expired(self, subscription: Subscription, bot: Bot | None) -> None:
        if bot is None or await self._notification_exists(
            subscription.id,
            subscription.expires_at,
            0,
        ):
            return
        try:
            await bot.send_message(
                subscription.user.telegram_id,
                (
                    f"Подписка на «{subscription.channel.title}» закончилась. "
                    "Право доступа завершено; обработка удаления выполняется автоматически."
                ),
            )
        except TelegramAPIError as exc:
            logger.warning(
                "Expiration notification failed subscription_id=%s error=%s",
                subscription.id,
                type(exc).__name__,
            )
            raise
        self.session.add(
            NotificationLog(
                user_id=subscription.user_id,
                subscription_id=subscription.id,
                type="expired",
                expires_at_snapshot=subscription.expires_at,
                threshold_minutes=0,
                sent_at=_utcnow(),
            )
        )

    async def _notification_exists(
        self,
        subscription_id: int,
        expires_at_snapshot: datetime,
        threshold_minutes: int,
    ) -> bool:
        return (
            await self.session.scalar(
                select(NotificationLog.id).where(
                    NotificationLog.subscription_id == subscription_id,
                    NotificationLog.expires_at_snapshot == expires_at_snapshot,
                    NotificationLog.threshold_minutes == threshold_minutes,
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


def _invite_expiration(
    now: datetime, subscription_expiration: datetime, ttl_minutes: int
) -> datetime:
    configured = now + timedelta(minutes=ttl_minutes)
    return min(max(configured, subscription_expiration), now + timedelta(days=366))


def _reminder_interval_text(minutes: int) -> str:
    if minutes % 1440 == 0:
        days = minutes // 1440
        suffix = "день" if days % 10 == 1 and days % 100 != 11 else "дней"
        if days % 10 in {2, 3, 4} and days % 100 not in {12, 13, 14}:
            suffix = "дня"
        return f"примерно через {days} {suffix}"
    if minutes % 60 == 0:
        hours = minutes // 60
        suffix = "час" if hours % 10 == 1 and hours % 100 != 11 else "часов"
        if hours % 10 in {2, 3, 4} and hours % 100 not in {12, 13, 14}:
            suffix = "часа"
        return f"примерно через {hours} {suffix}"
    return f"примерно через {minutes} мин."
