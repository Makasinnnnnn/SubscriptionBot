from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import func, select

from app.core.config import Settings
from app.db.enums import PaymentStatus
from app.db.models import (
    Channel,
    Payment,
    Plan,
    PromoCode,
    PromoRedemption,
    Subscription,
    User,
)
from app.payments.base import InvoiceResult
from app.services.legal_service import LegalService
from app.services.payment_service import PaymentService
from app.services.promo_service import (
    PromoRedemptionError,
    PromoService,
    discounted_amount,
)
from app.subscriptions.service import SubscriptionService


class FakeProvider:
    async def create_invoice(self, request):
        return InvoiceResult(
            f"fake-{request.payment_id}",
            "https://pay.example.test",
            {},
        )


class PromoBot:
    def __init__(self, fail_chat: int | None = None) -> None:
        self.fail_chat = fail_chat
        self.invites: list[int] = []
        self.revoked: list[tuple[int, str]] = []

    async def get_me(self):
        return SimpleNamespace(id=999)

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(
            status=ChatMemberStatus.ADMINISTRATOR,
            can_invite_users=True,
            can_restrict_members=True,
        )

    async def create_chat_invite_link(
        self,
        chat_id,
        name,
        expire_date,
        creates_join_request,
    ):
        if chat_id == self.fail_chat:
            raise TelegramBadRequest(method=None, message="failed")
        self.invites.append(chat_id)
        return SimpleNamespace(invite_link=f"https://t.me/+{name}")

    async def revoke_chat_invite_link(self, chat_id, invite_link):
        self.revoked.append((chat_id, invite_link))


async def _promo_fixture(
    session,
    *,
    valid_from=None,
    valid_until=None,
    limit=2,
    discount=20,
):
    now = _utcnow()
    user = User(telegram_id=500)
    channel = Channel(telegram_chat_id=-1001, title="Deals")
    session.add_all([user, channel])
    await session.flush()
    plan = Plan(
        channel_id=channel.id,
        title="Month",
        duration_days=30,
        price=Decimal(4990),
        currency="RUB",
        stars_price=500,
    )
    session.add(plan)
    await session.flush()
    promo = PromoCode(
        code="FLY2026",
        plan_id=plan.id,
        valid_from=valid_from or now - timedelta(hours=1),
        valid_until=valid_until or now + timedelta(days=1),
        discount_percent=discount,
        max_redemptions=limit,
        per_user_limit=1,
    )
    session.add(promo)
    await session.commit()
    return promo, plan, user


def _patch_payment_services(monkeypatch) -> Settings:
    settings = Settings(_env_file=None, app_env="test", legal_version="v1")
    monkeypatch.setattr(
        "app.services.legal_service.get_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        "app.services.payment_service.get_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        "app.services.payment_service.get_payment_provider",
        lambda _: FakeProvider(),
    )
    return settings


def _patch_subscription_settings(monkeypatch, materials_channel_id=None) -> None:
    settings = SimpleNamespace(
        materials_channel_id=materials_channel_id,
        invite_ttl_minutes=60,
        bot_token="",
        admin_ids=[],
        ops_bot_token="",
        ops_chat_ids=[],
    )
    monkeypatch.setattr(
        "app.subscriptions.service.get_settings",
        lambda: settings,
    )

    async def no_notify(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "app.subscriptions.service.SubscriptionService._notify_admins",
        no_notify,
    )


@pytest.mark.asyncio
async def test_promo_offer_quotes_discount_without_granting_access(session) -> None:
    promo, plan, _ = await _promo_fixture(session)

    offer = await PromoService(session).offer(500, "  fly2026 ")

    assert offer.promo.id == promo.id
    assert offer.promo.plan_id == plan.id
    assert offer.base_amount == Decimal(4990)
    assert offer.discounted_amount == Decimal("3992.00")
    assert offer.base_stars == 500
    assert offer.discounted_stars == 400
    assert await session.scalar(select(func.count(Subscription.id))) == 0
    assert await session.scalar(select(func.count(PromoRedemption.id))) == 0
    assert promo.redemptions_count == 0


@pytest.mark.asyncio
async def test_discounted_payment_reserves_promo_and_reuses_invoice(
    session,
    monkeypatch,
) -> None:
    settings = _patch_payment_services(monkeypatch)
    promo, plan, user = await _promo_fixture(session)
    await LegalService(session, settings).accept(user)
    await session.commit()

    payment = await PaymentService(session).create_payment(
        user,
        plan,
        "fake",
        promo_id=promo.id,
    )
    duplicate = await PaymentService(session).create_payment(
        user,
        plan,
        "fake",
        promo_id=promo.id,
    )

    assert duplicate.id == payment.id
    assert payment.promo_code_id == promo.id
    assert payment.amount == Decimal("3992.00")
    assert payment.metadata_json["base_amount"] == "4990"
    assert payment.metadata_json["promo_discount_percent"] == 20
    assert promo.redemptions_count == 0
    assert await session.scalar(select(func.count(PromoRedemption.id))) == 0


@pytest.mark.asyncio
async def test_promo_is_consumed_only_after_paid_access_is_provisioned(
    session,
) -> None:
    promo, plan, user = await _promo_fixture(session)
    payment = Payment(
        user_id=user.id,
        plan_id=plan.id,
        promo_code_id=promo.id,
        provider="fake",
        provider_payment_id="paid-1",
        status=PaymentStatus.PAID,
        amount=Decimal(3992),
        currency="RUB",
        idempotency_key="promo-paid-1",
    )
    subscription = Subscription(
        user_id=user.id,
        channel_id=plan.channel_id,
        payment=payment,
        starts_at=_utcnow(),
        expires_at=_utcnow() + timedelta(days=30),
    )
    session.add_all([payment, subscription])
    await session.flush()

    first = await PromoService(session).finalize_redemption(
        payment,
        subscription,
    )
    second = await PromoService(session).finalize_redemption(
        payment,
        subscription,
    )
    await session.commit()

    assert first.id == second.id
    assert first.payment_id == payment.id
    assert promo.redemptions_count == 1
    assert await session.scalar(select(func.count(PromoRedemption.id))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("from_delta", "until_delta", "message"),
    [
        (timedelta(hours=1), timedelta(days=1), "ещё не начал"),
        (-timedelta(days=2), -timedelta(hours=1), "закончился"),
    ],
)
async def test_promo_validity_window(
    session,
    from_delta,
    until_delta,
    message,
) -> None:
    now = _utcnow()
    await _promo_fixture(
        session,
        valid_from=now + from_delta,
        valid_until=now + until_delta,
    )

    with pytest.raises(PromoRedemptionError, match=message):
        await PromoService(session).offer(500, "FLY2026", now=now)


@pytest.mark.asyncio
async def test_pending_invoice_reserves_global_and_per_user_limits(
    session,
) -> None:
    promo, plan, user = await _promo_fixture(session, limit=1)
    session.add(
        Payment(
            user_id=user.id,
            plan_id=plan.id,
            promo_code_id=promo.id,
            provider="fake",
            provider_payment_id="pending-1",
            amount=Decimal(3992),
            currency="RUB",
            idempotency_key="promo-pending-1",
        )
    )
    second_user = User(telegram_id=501)
    session.add(second_user)
    await session.commit()

    with pytest.raises(PromoRedemptionError, match="Лимит"):
        await PromoService(session).offer(501, "FLY2026")

    promo.max_redemptions = 2
    await session.commit()
    with pytest.raises(PromoRedemptionError, match="создали по нему счёт"):
        await PromoService(session).ensure_capacity(
            promo,
            user.id,
            _utcnow(),
        )


@pytest.mark.asyncio
async def test_failed_materials_invite_does_not_consume_discount_promo(
    session,
    monkeypatch,
) -> None:
    materials_id = -1002
    _patch_subscription_settings(monkeypatch, materials_id)
    promo, plan, user = await _promo_fixture(session)
    payment = Payment(
        user_id=user.id,
        plan_id=plan.id,
        promo_code_id=promo.id,
        provider="fake",
        provider_payment_id="paid-failing",
        status=PaymentStatus.PAID,
        amount=Decimal(3992),
        currency="RUB",
        idempotency_key="promo-failing",
    )
    session.add(payment)
    await session.commit()

    bot = PromoBot(fail_chat=materials_id)
    with pytest.raises(TelegramBadRequest):
        async with session.begin_nested():
            await SubscriptionService(session).activate_from_payment(payment, bot)

    assert await session.scalar(select(func.count(PromoRedemption.id))) == 0
    assert await session.scalar(select(func.count(Subscription.id))) == 0
    await session.refresh(promo)
    assert promo.redemptions_count == 0
    assert bot.revoked == [(-1001, "https://t.me/+sub-1")]


@pytest.mark.asyncio
async def test_paid_discount_payment_records_redemption_after_access(
    session,
    monkeypatch,
) -> None:
    _patch_subscription_settings(monkeypatch)
    promo, plan, user = await _promo_fixture(session)
    payment = Payment(
        user_id=user.id,
        plan_id=plan.id,
        promo_code_id=promo.id,
        provider="fake",
        provider_payment_id="paid-success",
        status=PaymentStatus.PAID,
        amount=Decimal(3992),
        currency="RUB",
        idempotency_key="promo-success",
    )
    session.add(payment)
    await session.commit()

    subscription, _, _ = await SubscriptionService(session).activate_from_payment(
        payment,
        PromoBot(),
    )
    await session.commit()

    redemption = await session.scalar(
        select(PromoRedemption).where(PromoRedemption.payment_id == payment.id)
    )
    assert redemption is not None
    assert redemption.subscription_id == subscription.id
    assert promo.redemptions_count == 1


def test_discount_rounding_and_minimum_amount() -> None:
    assert discounted_amount(Decimal(4990), "RUB", 20) == Decimal("3992.00")
    assert discounted_amount(Decimal(101), "XTR", 25) == Decimal(76)
    assert discounted_amount(Decimal(1), "XTR", 99) == Decimal(1)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
