import re
from dataclasses import dataclass
from datetime import UTC, datetime

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.admin.settings_service import RuntimeSettingsService
from app.core.config import get_settings
from app.db.models import (
    Plan,
    PromoCode,
    PromoRedemption,
    Subscription,
    User,
)
from app.subscriptions.service import SubscriptionService

PROMO_PATTERN = re.compile(r"[A-Z0-9_-]{4,32}")


class PromoRedemptionError(ValueError):
    pass


@dataclass(frozen=True)
class PromoRedemptionResult:
    promo: PromoCode
    subscription: Subscription
    primary_link: str
    materials_link: str | None


def normalize_promo_code(raw: str) -> str:
    value = raw.strip().upper()
    if not PROMO_PATTERN.fullmatch(value):
        raise PromoRedemptionError(
            "Промокод должен содержать 4-32 латинских символа, цифры, '-' или '_'."
        )
    return value


class PromoService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def redeem(
        self,
        telegram_id: int,
        raw_code: str,
        bot: Bot,
        *,
        now: datetime | None = None,
    ) -> PromoRedemptionResult:
        code = normalize_promo_code(raw_code)
        checked_at = now or datetime.now(UTC).replace(tzinfo=None)
        promo = await self.session.scalar(
            select(PromoCode)
            .options(selectinload(PromoCode.plan).selectinload(Plan.channel))
            .where(PromoCode.code == code)
            .with_for_update()
        )
        if promo is None:
            raise PromoRedemptionError("Промокод не найден.")
        if not promo.is_active:
            raise PromoRedemptionError("Промокод отключён.")
        if checked_at < promo.valid_from:
            raise PromoRedemptionError("Промокод ещё не начал действовать.")
        if checked_at >= promo.valid_until:
            raise PromoRedemptionError("Срок действия промокода закончился.")
        if promo.redemptions_count >= promo.max_redemptions:
            raise PromoRedemptionError("Лимит применений промокода исчерпан.")
        if not promo.plan.is_active or not promo.plan.channel.is_active:
            raise PromoRedemptionError("Тариф промокода недоступен.")

        materials_channel_id = await RuntimeSettingsService(self.session).get(
            RuntimeSettingsService.MATERIALS_CHANNEL_ID,
            get_settings().materials_channel_id,
        )
        if (
            materials_channel_id is not None
            and promo.plan.channel.telegram_chat_id == int(materials_channel_id)
        ):
            raise PromoRedemptionError("Служебный канал нельзя выдать промокодом.")

        user = await self.session.scalar(
            select(User).where(User.telegram_id == telegram_id).with_for_update()
        )
        if user is None:
            user = User(telegram_id=telegram_id)
            self.session.add(user)
            await self.session.flush()

        previous_uses = await self.session.scalar(
            select(func.count(PromoRedemption.id)).where(
                PromoRedemption.promo_code_id == promo.id,
                PromoRedemption.user_id == user.id,
            )
        )
        if int(previous_uses or 0) >= promo.per_user_limit:
            raise PromoRedemptionError("Вы уже использовали этот промокод.")

        subscription, invite, materials = await SubscriptionService(
            self.session
        ).activate_from_promo(user, promo.plan, bot)
        self.session.add(
            PromoRedemption(
                promo_code_id=promo.id,
                user_id=user.id,
                subscription_id=subscription.id,
                redeemed_at=checked_at,
            )
        )
        promo.redemptions_count += 1
        await self.session.flush()
        return PromoRedemptionResult(
            promo=promo,
            subscription=subscription,
            primary_link=invite.link,
            materials_link=materials,
        )
