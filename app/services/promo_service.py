import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.enums import PaymentStatus
from app.db.models import (
    Payment,
    Plan,
    PromoCode,
    PromoRedemption,
    Subscription,
    User,
)

PROMO_PATTERN = re.compile(r"[A-Z0-9_-]{4,32}")
PROMO_RESERVATION_TTL = timedelta(hours=1)


class PromoRedemptionError(ValueError):
    pass


@dataclass(frozen=True)
class PromoOffer:
    promo: PromoCode
    base_amount: Decimal
    discounted_amount: Decimal
    currency: str
    base_stars: int | None
    discounted_stars: int | None


def normalize_promo_code(raw: str) -> str:
    value = raw.strip().upper()
    if not PROMO_PATTERN.fullmatch(value):
        raise PromoRedemptionError(
            "Промокод должен содержать 4-32 латинских символа, цифры, '-' или '_'."
        )
    return value


def discounted_amount(
    base_amount: Decimal,
    currency: str,
    discount_percent: int,
) -> Decimal:
    if not 1 <= discount_percent <= 99:
        raise PromoRedemptionError("Скидка должна быть от 1% до 99%.")
    factor = Decimal(100 - discount_percent) / Decimal(100)
    if currency.upper() == "XTR":
        return max(
            Decimal(1),
            (base_amount * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP),
        )
    return max(
        Decimal("0.01"),
        (base_amount * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
    )


class PromoService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def offer(
        self,
        telegram_id: int,
        raw_code: str,
        *,
        now: datetime | None = None,
    ) -> PromoOffer:
        checked_at = now or _utcnow()
        code = normalize_promo_code(raw_code)
        promo = await self.session.scalar(
            select(PromoCode)
            .options(selectinload(PromoCode.plan).selectinload(Plan.channel))
            .where(PromoCode.code == code)
        )
        self._validate_static(promo, checked_at)
        user_id = await self.session.scalar(
            select(User.id).where(User.telegram_id == telegram_id)
        )
        await self.ensure_capacity(
            promo,
            user_id,
            checked_at,
            allow_existing_reservation=True,
        )
        stars_price = promo.plan.stars_price
        return PromoOffer(
            promo=promo,
            base_amount=Decimal(promo.plan.price),
            discounted_amount=discounted_amount(
                Decimal(promo.plan.price),
                promo.plan.currency,
                promo.discount_percent,
            ),
            currency=promo.plan.currency,
            base_stars=stars_price,
            discounted_stars=(
                int(
                    discounted_amount(
                        Decimal(stars_price),
                        "XTR",
                        promo.discount_percent,
                    )
                )
                if stars_price
                else None
            ),
        )

    async def load_for_payment(
        self,
        *,
        promo_id: int,
        plan_id: int,
        now: datetime,
    ) -> PromoCode:
        promo = await self.session.scalar(
            select(PromoCode)
            .options(selectinload(PromoCode.plan).selectinload(Plan.channel))
            .where(PromoCode.id == promo_id)
            .with_for_update()
        )
        self._validate_static(promo, now)
        if promo.plan_id != plan_id:
            raise PromoRedemptionError("Промокод предназначен для другого тарифа.")
        return promo

    async def ensure_capacity(
        self,
        promo: PromoCode,
        user_id: int | None,
        now: datetime,
        *,
        allow_existing_reservation: bool = False,
    ) -> None:
        cutoff = now - PROMO_RESERVATION_TTL
        redeemed_count = 0
        pending_for_user = 0
        if user_id is not None:
            redeemed_count = int(
                await self.session.scalar(
                    select(func.count(PromoRedemption.id)).where(
                        PromoRedemption.promo_code_id == promo.id,
                        PromoRedemption.user_id == user_id,
                    )
                )
                or 0
            )
            pending_for_user = int(
                await self.session.scalar(
                    select(func.count(Payment.id)).where(
                        Payment.promo_code_id == promo.id,
                        Payment.user_id == user_id,
                        Payment.status == PaymentStatus.PENDING,
                        Payment.created_at >= cutoff,
                    )
                )
                or 0
            )
            if redeemed_count >= promo.per_user_limit:
                raise PromoRedemptionError("Вы уже использовали этот промокод.")
            if allow_existing_reservation and pending_for_user:
                return
        pending_count = int(
            await self.session.scalar(
                select(func.count(Payment.id)).where(
                    Payment.promo_code_id == promo.id,
                    Payment.status == PaymentStatus.PENDING,
                    Payment.created_at >= cutoff,
                )
            )
            or 0
        )
        if promo.redemptions_count + pending_count >= promo.max_redemptions:
            raise PromoRedemptionError("Лимит применений промокода исчерпан.")
        if user_id is None:
            return
        if (
            redeemed_count + pending_for_user
            >= promo.per_user_limit
        ):
            raise PromoRedemptionError(
                "Вы уже использовали этот промокод или создали по нему счёт."
            )

    async def finalize_redemption(
        self,
        payment: Payment,
        subscription: Subscription,
        *,
        now: datetime | None = None,
    ) -> PromoRedemption | None:
        if payment.promo_code_id is None:
            return None
        existing = await self.session.scalar(
            select(PromoRedemption).where(
                PromoRedemption.payment_id == payment.id
            )
        )
        if existing is not None:
            return existing
        promo = await self.session.scalar(
            select(PromoCode)
            .where(PromoCode.id == payment.promo_code_id)
            .with_for_update()
        )
        if promo is None or promo.plan_id != payment.plan_id:
            raise PromoRedemptionError("Промокод платежа больше не существует.")
        redemption = PromoRedemption(
            promo_code_id=promo.id,
            user_id=payment.user_id,
            payment_id=payment.id,
            subscription_id=subscription.id,
            redeemed_at=now or _utcnow(),
        )
        self.session.add(redemption)
        promo.redemptions_count += 1
        await self.session.flush()
        return redemption

    @staticmethod
    def _validate_static(
        promo: PromoCode | None,
        checked_at: datetime,
    ) -> None:
        if promo is None:
            raise PromoRedemptionError("Промокод не найден.")
        if not promo.is_active:
            raise PromoRedemptionError("Промокод отключён.")
        if checked_at < promo.valid_from:
            raise PromoRedemptionError("Промокод ещё не начал действовать.")
        if checked_at >= promo.valid_until:
            raise PromoRedemptionError("Срок действия промокода закончился.")
        if not promo.plan.is_active or not promo.plan.channel.is_active:
            raise PromoRedemptionError("Тариф промокода недоступен.")


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
