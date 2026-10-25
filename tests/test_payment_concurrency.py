import asyncio
import os
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_subscription_service import FakeBot

from app.core.config import Settings
from app.db.base import Base
from app.db.enums import PaymentStatus
from app.db.models import Channel, Payment, PaymentApplication, Plan, Subscription, User
from app.subscriptions.service import SubscriptionService


@pytest_asyncio.fixture
async def postgres_factory(monkeypatch):
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required for real PostgreSQL locking tests")
    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail("TEST_POSTGRES_URL must use PostgreSQL/asyncpg")
    schema = "audit_" + uuid4().hex
    control = create_async_engine(url)
    async with control.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    settings = Settings(
        _env_file=None,
        app_env="test",
        materials_channel_id=None,
        admin_ids=[],
        ops_chat_ids=[],
        ops_bot_token="",
    )
    monkeypatch.setattr("app.subscriptions.service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.admin_notification_service.get_settings", lambda: settings)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with control.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await control.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("same_payment", [True, False])
async def test_postgres_parallel_payment_application_is_atomic(postgres_factory, same_payment):
    async with postgres_factory() as session:
        user = User(telegram_id=990001)
        channel = Channel(telegram_chat_id=-100990001, title="Synthetic concurrency")
        session.add_all([user, channel])
        await session.flush()
        plan = Plan(
            channel_id=channel.id,
            title="30 days",
            duration_days=30,
            price=Decimal(10),
            currency="USD",
        )
        session.add(plan)
        await session.flush()
        payments = [
            Payment(
                user_id=user.id,
                plan_id=plan.id,
                provider="dev",
                status=PaymentStatus.PAID,
                amount=Decimal(10),
                currency="USD",
                provider_payment_id=f"audit-{i}",
                idempotency_key=f"audit-{i}",
                purchased_duration_days=30,
                purchased_channel_id=channel.id,
            )
            for i in range(2)
        ]
        session.add_all(payments)
        await session.commit()
        ids = [payments[0].id, payments[0].id if same_payment else payments[1].id]
    barrier = asyncio.Event()

    async def apply(payment_id):
        async with postgres_factory() as session:
            payment = await session.get(Payment, payment_id)
            await barrier.wait()
            await SubscriptionService(session).activate_from_payment(payment, FakeBot())
            await session.commit()

    tasks = [asyncio.create_task(apply(payment_id)) for payment_id in ids]
    barrier.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=20)
    async with postgres_factory() as session:
        subscriptions = list(await session.scalars(select(Subscription)))
        assert len(subscriptions) == 1
        expected_days = 30 if same_payment else 60
        assert subscriptions[0].expires_at - subscriptions[0].starts_at == timedelta(
            days=expected_days
        )
        assert await session.scalar(select(func.count(PaymentApplication.id))) == (
            1 if same_payment else 2
        )


@pytest.mark.asyncio
async def test_postgres_refund_prevents_stale_unfulfilled_payment_worker(postgres_factory):
    from app.services.refund_service import RefundService

    async with postgres_factory() as session:
        user = User(telegram_id=990002)
        channel = Channel(telegram_chat_id=-100990002, title="Synthetic refund race")
        session.add_all([user, channel])
        await session.flush()
        plan = Plan(
            channel_id=channel.id,
            title="30 days",
            duration_days=30,
            price=Decimal(10),
            currency="USD",
        )
        session.add(plan)
        await session.flush()
        payment = Payment(
            user_id=user.id,
            plan_id=plan.id,
            provider="dev",
            status=PaymentStatus.PAID,
            amount=Decimal(10),
            currency="USD",
            provider_payment_id="audit-race",
            idempotency_key="audit-race",
            purchased_duration_days=30,
            purchased_channel_id=channel.id,
        )
        session.add(payment)
        await session.commit()
        payment_id = payment.id
    loaded = asyncio.Event()
    refunded = asyncio.Event()

    async def stale_worker():
        async with postgres_factory() as session:
            payment = await session.get(Payment, payment_id)
            assert payment.status == PaymentStatus.PAID
            loaded.set()
            await refunded.wait()
            with pytest.raises(ValueError, match="Refunded"):
                await SubscriptionService(session).activate_from_payment(payment, FakeBot())
            await session.rollback()

    task = asyncio.create_task(stale_worker())
    await asyncio.wait_for(loaded.wait(), 5)
    async with postgres_factory() as session:
        payment = await session.get(Payment, payment_id)
        await RefundService(session).apply_confirmed(payment, FakeBot())
        await session.commit()
    refunded.set()
    await asyncio.wait_for(task, 5)
    async with postgres_factory() as session:
        payment = await session.get(Payment, payment_id)
        assert payment.status == PaymentStatus.REFUNDED
        assert await session.scalar(select(func.count(PaymentApplication.id))) == 0
        assert await session.scalar(select(func.count(Subscription.id))) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("quota", ["global", "per_user"])
async def test_postgres_parallel_promo_reservations_cannot_oversell(
    postgres_factory, monkeypatch, quota
):
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.db.models import PromoCode
    from app.payments.base import InvoiceResult
    from app.services.legal_service import LegalService
    from app.services.payment_service import PaymentService
    from app.services.promo_service import PromoRedemptionError

    now = datetime.now(UTC).replace(tzinfo=None)
    settings = Settings(_env_file=None, app_env="test", materials_channel_id=None)
    monkeypatch.setattr("app.services.payment_service.get_settings", lambda: settings)
    monkeypatch.setattr(LegalService, "require", AsyncMock())

    async def invoice(request):
        return InvoiceResult(f"invoice-{request.payment_id}", "https://example.test", {})

    provider = SimpleNamespace(create_invoice=AsyncMock(side_effect=invoice))
    monkeypatch.setattr("app.services.payment_service.get_payment_provider", lambda _: provider)
    async with postgres_factory() as session:
        users = [User(telegram_id=991001), User(telegram_id=991002)]
        channel = Channel(telegram_chat_id=-100991001, title="Synthetic promo contention")
        session.add_all([*users, channel])
        await session.flush()
        plan = Plan(
            channel_id=channel.id,
            title="30 days",
            duration_days=30,
            price=Decimal(10),
            currency="USD",
        )
        session.add(plan)
        await session.flush()
        promo = PromoCode(
            code="RACE",
            plan_id=plan.id,
            discount_percent=20,
            valid_from=now - timedelta(days=1),
            valid_until=now + timedelta(days=1),
            max_redemptions=1 if quota == "global" else 2,
            per_user_limit=1,
        )
        session.add(promo)
        await session.commit()
        plan_id, promo_id = plan.id, promo.id
        ids = [users[0].id, users[1].id if quota == "global" else users[0].id]

    async def create(index, user_id):
        async with postgres_factory() as session:
            user = await session.get(User, user_id)
            plan = await session.get(Plan, plan_id)
            try:
                await PaymentService(session).create_payment(
                    user, plan, "dev" if index == 0 else "yookassa", promo_id=promo_id
                )
                return True
            except PromoRedemptionError:
                await session.rollback()
                return False

    results = await asyncio.wait_for(
        asyncio.gather(*[create(index, user_id) for index, user_id in enumerate(ids)]), timeout=20
    )
    assert sorted(results) == [False, True]
    assert provider.create_invoice.await_count == 1
    async with postgres_factory() as session:
        assert await session.scalar(select(func.count(Payment.id))) == 1


@pytest.mark.asyncio
async def test_postgres_parallel_refund_requests_create_one_external_refund(
    postgres_factory, monkeypatch
):
    from app.db.models import Refund
    from app.services.refund_service import RefundService

    async with postgres_factory() as session:
        user = User(telegram_id=990003)
        channel = Channel(telegram_chat_id=-100990003, title="Synthetic parallel refund")
        session.add_all([user, channel])
        await session.flush()
        plan = Plan(
            channel_id=channel.id,
            title="30 days",
            duration_days=30,
            price=Decimal(10),
            currency="USD",
        )
        session.add(plan)
        await session.flush()
        payment = Payment(
            user_id=user.id,
            plan_id=plan.id,
            provider="yookassa",
            status=PaymentStatus.PAID,
            amount=Decimal(10),
            currency="USD",
            provider_payment_id="parallel-refund",
            idempotency_key="parallel-refund",
            purchased_duration_days=30,
            purchased_channel_id=channel.id,
        )
        session.add(payment)
        await session.commit()
        payment_id = payment.id

    started = asyncio.Event()
    release = asyncio.Event()

    async def create_refund(provider_id, amount, key, *, expected_currency):
        assert provider_id == "parallel-refund"
        assert amount == Decimal(10) and expected_currency == "USD"
        assert key
        started.set()
        await release.wait()
        return {
            "id": "refund-once",
            "status": "succeeded",
            "payment_id": provider_id,
            "amount": {"value": "10", "currency": expected_currency},
        }

    provider = SimpleNamespace(create_refund=AsyncMock(side_effect=create_refund))
    monkeypatch.setattr("app.services.refund_service.get_payment_provider", lambda _: provider)

    async def first_request():
        async with postgres_factory() as session:
            payment = await session.get(Payment, payment_id)
            return await RefundService(session).request(payment)

    task = asyncio.create_task(first_request())
    try:
        await asyncio.wait_for(started.wait(), 5)
        async with postgres_factory() as session:
            payment = await session.get(Payment, payment_id)
            with pytest.raises(ValueError, match="in progress"):
                await RefundService(session).request(payment)
            await session.rollback()
    finally:
        release.set()
    assert await asyncio.wait_for(task, 5) is True
    assert provider.create_refund.await_count == 1
    async with postgres_factory() as session:
        assert await session.scalar(select(func.count(Refund.id))) == 1
