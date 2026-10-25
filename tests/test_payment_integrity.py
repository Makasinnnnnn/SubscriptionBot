from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.types import SuccessfulPayment
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_subscription_service import FakeBot

from app.core.config import Settings
from app.db.base import Base
from app.db.enums import PaymentStatus, SubscriptionStatus
from app.db.models import Channel, Payment, Plan, PromoCode, Subscription, User
from app.payments.base import InvoiceResult
from app.services.legal_service import LegalService
from app.services.payment_service import PaymentService
from app.services.promo_service import PromoRedemptionError, PromoService, discounted_amount
from app.subscriptions.service import SubscriptionService


@pytest_asyncio.fixture
async def db(monkeypatch):
    settings = Settings(
        _env_file=None,
        app_env="test",
        materials_channel_id=None,
        database_url="sqlite+aiosqlite:///:memory:",
        admin_ids=[],
        ops_chat_ids=[],
        ops_bot_token="",
    )
    monkeypatch.setattr("app.services.payment_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.subscriptions.service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.admin_notification_service.get_settings", lambda: settings)
    monkeypatch.setattr(LegalService, "require", AsyncMock(return_value=None))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def seed(db):
    user = User(telegram_id=900100)
    channel = Channel(telegram_chat_id=-100900100, title="Synthetic audit channel")
    db.add_all([user, channel])
    await db.flush()
    plan = Plan(
        channel_id=channel.id,
        title="30 days",
        duration_days=30,
        price=Decimal(10),
        currency="USD",
        stars_price=10,
    )
    db.add(plan)
    await db.commit()
    return user, channel, plan


async def payment(
    db, user, plan, name, *, status=PaymentStatus.PAID, provider="dev", promo=None, created_at=None
):
    p = Payment(
        user_id=user.id,
        plan_id=plan.id,
        provider=provider,
        amount=Decimal(10),
        currency="XTR" if provider == "telegram-stars" else "USD",
        status=status,
        idempotency_key=name,
        provider_payment_id=name,
        promo_code_id=promo.id if promo else None,
    )
    if created_at:
        p.created_at = created_at
    db.add(p)
    await db.commit()
    return p


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["service", "user_check", "admin_retry"])
async def test_payment_a_b_replay_does_not_add_another_period(db, monkeypatch, route):
    user, _channel, plan = await seed(db)
    a = await payment(db, user, plan, "A", provider="cryptobot")
    b = await payment(db, user, plan, "B", provider="cryptobot")
    service = SubscriptionService(db)
    bot = FakeBot()
    sub, *_ = await service.activate_from_payment(a, bot)
    await db.commit()
    await service.activate_from_payment(b, bot)
    await db.commit()
    paid_expiry = sub.expires_at
    if route == "service":
        await service.activate_from_payment(a, bot)
        await db.commit()
    else:
        from app.bot.handlers import admin_reports as admin_handler
        from app.bot.handlers import subscriptions as user_handler
        from app.payments.base import PaymentStatusResult

        aid, sid, tg = a.id, sub.id, user.telegram_id
        db.expunge_all()

        @asynccontextmanager
        async def factory():
            yield db

        handler = user_handler if route == "user_check" else admin_handler
        monkeypatch.setattr(handler, "async_session_factory", factory)
        monkeypatch.setattr(admin_handler, "AdminPermissionService", Permissions)
        provider = SimpleNamespace(
            check_payment_status=AsyncMock(
                return_value=PaymentStatusResult("A", PaymentStatus.PAID, Decimal(10), "USD", {})
            )
        )
        monkeypatch.setattr("app.services.payment_service.get_payment_provider", lambda _: provider)
        callback = SimpleNamespace(
            data=f"payments:status:{aid}" if route == "user_check" else f"rpt:provision:{aid}",
            from_user=SimpleNamespace(id=tg),
            bot=bot,
            message=SimpleNamespace(edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        await (
            handler.payment_status(callback)
            if route == "user_check"
            else handler.payment_retry_provision(callback)
        )
        sub = await db.get(Subscription, sid)
    assert sub.expires_at == paid_expiry, f"replayed A adds {sub.expires_at - paid_expiry}"


@pytest.mark.asyncio
async def test_invoice_snapshots_duration_before_plan_edit(db, monkeypatch):
    user, _channel, plan = await seed(db)
    provider = SimpleNamespace(
        create_invoice=AsyncMock(return_value=InvoiceResult("external", "https://example.test", {}))
    )
    monkeypatch.setattr("app.services.payment_service.get_payment_provider", lambda _: provider)
    p = await PaymentService(db).create_payment(user, plan, "dev")
    plan.duration_days = 3
    await db.commit()
    sub, *_ = await SubscriptionService(db).activate_from_payment(p, FakeBot())
    assert sub.expires_at - sub.period_started_at == timedelta(days=30)


@pytest.mark.asyncio
async def test_provider_timeout_retains_attempt_and_stable_key(db, monkeypatch):
    user, _channel, plan = await seed(db)
    uid, pid = user.id, plan.id
    requests = []

    async def create(request):
        requests.append(request)
        if len(requests) == 1:
            raise TimeoutError("synthetic: provider accepted invoice, response lost")
        return InvoiceResult("external-second", "https://example.test", {})

    monkeypatch.setattr(
        "app.services.payment_service.get_payment_provider",
        lambda _: SimpleNamespace(create_invoice=create),
    )
    with pytest.raises(TimeoutError):
        await PaymentService(db).create_payment(user, plan, "dev")
    retained = await db.scalar(select(func.count(Payment.id)))
    user = await db.get(User, uid)
    plan = await db.get(Plan, pid)
    await PaymentService(db).create_payment(user, plan, "dev")
    assert retained == 1 and requests[0].idempotency_key == requests[1].idempotency_key, (
        f"local_attempts_after_timeout={retained}; stable_key={requests[0].idempotency_key == requests[1].idempotency_key}"
    )


class Message:
    def __init__(self, user, p, *, currency="XTR", total=10):
        self.from_user = SimpleNamespace(id=user.telegram_id)
        self.bot = FakeBot()
        self.answers = []
        self.successful_payment = SuccessfulPayment(
            invoice_payload=f"payment:{p.id}",
            telegram_payment_charge_id=f"charge-{p.id}",
            currency=currency,
            total_amount=total,
            provider_payment_charge_id="synthetic",
        )

    async def answer(self, text, **kwargs):
        self.answers.append(text)


@pytest.mark.asyncio
async def test_expired_stars_success_is_recorded_and_delivered(db, monkeypatch):
    from app.bot.handlers import subscriptions as handler

    user, _channel, plan = await seed(db)
    p = await payment(
        db, user, plan, "stars-expired", status=PaymentStatus.EXPIRED, provider="telegram-stars"
    )

    @asynccontextmanager
    async def factory():
        yield db

    monkeypatch.setattr(handler, "async_session_factory", factory)
    msg = Message(user, p)
    await handler.telegram_stars_successful_payment(msg)
    assert p.status == PaymentStatus.PAID, msg.answers


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", ["amount", "currency", "user"])
async def test_stars_rejects_mismatched_identity_amount_currency(db, monkeypatch, wrong):
    from app.bot.handlers import subscriptions as handler

    user, _channel, plan = await seed(db)
    p = await payment(
        db, user, plan, "stars-pending", status=PaymentStatus.PENDING, provider="telegram-stars"
    )

    @asynccontextmanager
    async def factory():
        yield db

    monkeypatch.setattr(handler, "async_session_factory", factory)
    msg = Message(
        user,
        p,
        currency="USD" if wrong == "currency" else "XTR",
        total=11 if wrong == "amount" else 10,
    )
    if wrong == "user":
        msg.from_user.id += 1
    await handler.telegram_stars_successful_payment(msg)
    assert p.status == PaymentStatus.PENDING
    assert await db.scalar(select(func.count(Subscription.id))) == 0


class Permissions:
    def __init__(self, session):
        pass

    async def can_manage(self, *args):
        return True

    async def can_manage_channel(self, *args):
        return True

    async def audit(self, *args):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["old", "latest"])
async def test_refund_preserves_other_paid_period_and_manual_days(db, monkeypatch, which):
    from app.bot.handlers import admin as handler

    user, _channel, plan = await seed(db)
    a = await payment(db, user, plan, "refund-A")
    b = await payment(db, user, plan, "refund-B")
    service = SubscriptionService(db)
    bot = FakeBot()
    sub, *_ = await service.activate_from_payment(a, bot)
    await service.activate_from_payment(b, bot)
    await service.extend_manual(sub.id, 5, bot)
    await db.commit()
    before = sub.expires_at

    @asynccontextmanager
    async def factory():
        yield db

    monkeypatch.setattr(handler, "async_session_factory", factory)
    monkeypatch.setattr(handler, "AdminPermissionService", Permissions)
    p = a if which == "old" else b
    pid, sid = p.id, sub.id
    db.expunge_all()
    callback = SimpleNamespace(
        data=f"refund:{pid}",
        from_user=SimpleNamespace(id=1),
        bot=bot,
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    await handler.admin_refund_confirm(callback)
    p = await db.get(Payment, pid)
    sub = await db.get(Subscription, sid)
    assert p.status == PaymentStatus.REFUNDED
    assert (
        sub.status == SubscriptionStatus.ACTIVE
        and abs((sub.expires_at - (before - timedelta(days=30))).total_seconds()) < 1
    ), f"{which}: status={sub.status}; days_removed={(before - sub.expires_at).days}"


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["global", "per_user"])
async def test_expired_promo_reservation_cannot_overconsume_limit(db, limit):
    user, _channel, plan = await seed(db)
    now = datetime.now(UTC).replace(tzinfo=None)
    promo = PromoCode(
        code="AUDIT",
        plan_id=plan.id,
        discount_percent=20,
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=1),
        max_redemptions=1 if limit == "global" else 2,
        per_user_limit=1,
    )
    db.add(promo)
    await db.commit()
    await payment(
        db,
        user,
        plan,
        "promo-old",
        status=PaymentStatus.PENDING,
        promo=promo,
        created_at=now - timedelta(hours=2),
    )
    other = User(telegram_id=900200) if limit == "global" else user
    if limit == "global":
        db.add(other)
        await db.commit()
    with pytest.raises(PromoRedemptionError):
        await PromoService(db).ensure_capacity(promo, other.id, now)


@pytest.mark.parametrize(
    "currency,base,discount,expected",
    [
        ("USD", "0.01", 99, "0.01"),
        ("XTR", "1", 99, "1"),
        ("USD", "10", 33, "6.70"),
        ("XTR", "5", 50, "3"),
    ],
)
def test_rounding(currency, base, discount, expected):
    assert discounted_amount(Decimal(base), currency, discount) == Decimal(expected)


def test_crypto_discount_never_increases_price():
    result = discounted_amount(Decimal("0.0001"), "BTC", 20)
    assert result <= Decimal("0.0001"), f"BTC invoice discount increases 0.0001 to {result}"


@pytest.mark.asyncio
async def test_crypto_provider_preserves_asset_precision(monkeypatch):
    from app.payments.base import InvoiceRequest
    from app.payments.providers import CryptoBotProvider

    provider = CryptoBotProvider("synthetic-token", "https://never-called.invalid")
    request = AsyncMock(return_value={"invoice_id": 1, "bot_invoice_url": "https://example.test"})
    monkeypatch.setattr(provider, "_request", request)
    await provider.create_invoice(
        InvoiceRequest(1, 1, 1, Decimal("0.0001"), "BTC", "audit", "audit", {})
    )
    body = request.call_args.args[1]
    assert Decimal(body["amount"]) == Decimal("0.0001"), body


@pytest.mark.asyncio
async def test_confirmed_refund_is_idempotent_and_preserves_manual_days(db, monkeypatch):
    from app.services import refund_service
    from app.services.refund_service import RefundService

    started = datetime(2026, 1, 1)
    monkeypatch.setattr("app.subscriptions.service._utcnow", lambda: started)
    user, _channel, plan = await seed(db)
    a = await payment(db, user, plan, "consumed-A")
    b = await payment(db, user, plan, "consumed-B")
    bot = FakeBot()
    service = SubscriptionService(db)
    sub, *_ = await service.activate_from_payment(a, bot)
    await service.activate_from_payment(b, bot)
    await service.extend_manual(sub.id, 5, bot)
    await db.commit()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = started + timedelta(days=20)
            return value.replace(tzinfo=UTC) if tz else value

    monkeypatch.setattr(refund_service, "datetime", Clock)
    await RefundService(db).apply_confirmed(a, bot)
    await db.commit()
    assert sub.expires_at == started + timedelta(days=55)
    await RefundService(db).apply_confirmed(a, bot)
    await db.commit()
    assert sub.expires_at == started + timedelta(days=55)
    await RefundService(db).apply_confirmed(b, bot)
    await db.commit()
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.expires_at == started + timedelta(days=25)


@pytest.mark.asyncio
async def test_pending_refund_reconciles_external_id_without_second_create(db, monkeypatch):
    from app.db.models import Refund
    from app.services.refund_service import RefundService

    user, _channel, plan = await seed(db)
    p = await payment(db, user, plan, "pending-refund", provider="yookassa")
    provider = SimpleNamespace(
        create_refund=AsyncMock(
            return_value={
                "id": "refund-1",
                "status": "pending",
                "payment_id": p.provider_payment_id,
                "amount": {"value": "10", "currency": "USD"},
            }
        ),
        check_refund=AsyncMock(
            return_value={
                "id": "refund-1",
                "status": "succeeded",
                "payment_id": p.provider_payment_id,
                "amount": {"value": "10", "currency": "USD"},
            }
        ),
    )
    monkeypatch.setattr("app.services.refund_service.get_payment_provider", lambda _: provider)
    service = RefundService(db)
    assert await service.request(p) is False
    row = await db.scalar(select(Refund))
    assert row.external_id == "refund-1" and row.state == "pending"
    assert p.status == PaymentStatus.PAID
    assert await service.request(p) is True
    assert p.status == PaymentStatus.REFUNDED
    assert provider.create_refund.await_count == 1
    provider.check_refund.assert_awaited_once_with("refund-1")


@pytest.mark.asyncio
async def test_unknown_invoice_outside_idempotence_window_cannot_create_new(db, monkeypatch):
    user, _channel, plan = await seed(db)
    p = await payment(
        db,
        user,
        plan,
        "uncertain-old",
        provider="yookassa",
        status=PaymentStatus.PENDING,
        created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2),
    )
    p.metadata_json = {"invoice_state": "unknown"}
    p.provider_payment_id = None
    await db.commit()
    provider = SimpleNamespace(create_invoice=AsyncMock())
    monkeypatch.setattr("app.services.payment_service.get_payment_provider", lambda _: provider)
    with pytest.raises((ValueError, RuntimeError), match="reconcil"):
        await PaymentService(db).create_payment(user, plan, "yookassa")
    provider.create_invoice.assert_not_awaited()
    assert await db.scalar(select(func.count(Payment.id))) == 1


@pytest.mark.asyncio
async def test_refunded_unfulfilled_payment_never_grants_access(db):
    user, _channel, plan = await seed(db)
    p = await payment(db, user, plan, "already-refunded", status=PaymentStatus.REFUNDED)
    with pytest.raises(ValueError, match="Refunded"):
        await SubscriptionService(db).activate_from_payment(p, FakeBot())
    assert await db.scalar(select(func.count(Subscription.id))) == 0


@pytest.mark.asyncio
async def test_new_checkout_does_not_reuse_invoice_with_different_duration(db, monkeypatch):
    user, _channel, plan = await seed(db)

    async def create(request):
        return InvoiceResult(f"external-{request.payment_id}", "https://example.test", {})

    provider = SimpleNamespace(create_invoice=AsyncMock(side_effect=create))
    monkeypatch.setattr("app.services.payment_service.get_payment_provider", lambda _: provider)
    first = await PaymentService(db).create_payment(user, plan, "dev")
    plan.duration_days = 60
    await db.commit()
    second = await PaymentService(db).create_payment(user, plan, "dev")
    assert second.id != first.id
    assert first.purchased_duration_days == 30 and second.purchased_duration_days == 60


@pytest.mark.asyncio
async def test_uncertain_invoice_two_hours_old_can_resume_same_intent(db, monkeypatch):
    user, channel, plan = await seed(db)
    p = await payment(
        db,
        user,
        plan,
        "uncertain-two-hours",
        provider="yookassa",
        status=PaymentStatus.PENDING,
        created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2),
    )
    p.metadata_json = {"invoice_state": "unknown"}
    p.provider_payment_id = None
    p.purchased_duration_days = 30
    p.purchased_channel_id = channel.id
    await db.commit()
    provider = SimpleNamespace(
        create_invoice=AsyncMock(
            return_value=InvoiceResult("recovered", "https://example.test", {})
        )
    )
    monkeypatch.setattr("app.services.payment_service.get_payment_provider", lambda _: provider)
    recovered = await PaymentService(db).create_payment(user, plan, "yookassa")
    assert recovered.id == p.id
    assert provider.create_invoice.call_args.args[0].idempotency_key == "uncertain-two-hours"
    assert await db.scalar(select(func.count(Payment.id))) == 1


@pytest.mark.asyncio
async def test_pending_payment_cannot_create_external_refund(db, monkeypatch):
    from app.services.refund_service import RefundService

    user, _channel, plan = await seed(db)
    p = await payment(
        db, user, plan, "not-charged", provider="yookassa", status=PaymentStatus.PENDING
    )
    provider = SimpleNamespace(create_refund=AsyncMock())
    monkeypatch.setattr("app.services.refund_service.get_payment_provider", lambda _: provider)
    with pytest.raises(ValueError, match="confirmed"):
        await RefundService(db).request(p)
    provider.create_refund.assert_not_awaited()
