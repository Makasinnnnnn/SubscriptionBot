from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.admin.reconciliation import PurchaseReconciliationService
from app.core.config import Settings
from app.db.enums import PaymentStatus
from app.db.models import (
    AccessOperation,
    AuditLog,
    Channel,
    Payment,
    PaymentApplication,
    Plan,
    Subscription,
    User,
)


async def legacy_payment(session, monkeypatch):
    settings = Settings(_env_file=None, app_env="test", admin_ids=[42])
    monkeypatch.setattr("app.admin.service.get_settings", lambda: settings)
    user = User(telegram_id=9009001)
    channel = Channel(telegram_chat_id=-1009009001, title="Synthetic verified channel")
    session.add_all([user, channel])
    await session.flush()
    plan = Plan(
        channel_id=channel.id,
        title="Changed tariff",
        duration_days=3,
        price=Decimal(10),
        currency="USD",
    )
    session.add(plan)
    await session.flush()
    payment = Payment(
        user_id=user.id,
        plan_id=plan.id,
        provider="dev",
        amount=Decimal(10),
        currency="USD",
        status=PaymentStatus.PAID,
        idempotency_key="legacy-reconciliation",
        purchased_duration_days=3,
        purchased_channel_id=channel.id,
        metadata_json={"terms_origin": "migration_catalog_unverified"},
    )
    session.add(payment)
    await session.commit()
    return payment, channel


@pytest.mark.asyncio
async def test_reconciliation_preview_does_not_write(session, monkeypatch):
    payment, channel = await legacy_payment(session, monkeypatch)
    result = await PurchaseReconciliationService(session).reconcile(
        actor_id=42,
        payment_id=payment.id,
        channel_id=channel.id,
        duration_days=30,
        reference="Synthetic receipt",
    )
    assert result["after"]["duration_days"] == 30
    assert payment.purchased_duration_days == 3
    assert await session.scalar(select(func.count(AuditLog.id))) == 0
    assert await session.scalar(select(func.count(AccessOperation.id))) == 0


@pytest.mark.asyncio
async def test_reconciliation_requires_actor_and_explicit_unapplied_confirmation(
    session, monkeypatch
):
    payment, channel = await legacy_payment(session, monkeypatch)
    arguments = {
        "payment_id": payment.id,
        "channel_id": channel.id,
        "duration_days": 30,
        "reference": "Synthetic receipt",
        "apply": True,
    }
    with pytest.raises(PermissionError):
        await PurchaseReconciliationService(session).reconcile(
            actor_id=99, confirmed_unapplied=True, **arguments
        )
    with pytest.raises(ValueError, match="подтвердите"):
        await PurchaseReconciliationService(session).reconcile(actor_id=42, **arguments)
    assert payment.purchased_duration_days == 3


@pytest.mark.asyncio
async def test_verified_terms_and_retry_are_committed_together(session, monkeypatch):
    payment, channel = await legacy_payment(session, monkeypatch)
    await PurchaseReconciliationService(session).reconcile(
        actor_id=42,
        payment_id=payment.id,
        channel_id=channel.id,
        duration_days=30,
        reference="Synthetic receipt",
        apply=True,
        confirmed_unapplied=True,
    )
    await session.commit()
    assert payment.purchased_duration_days == 30
    assert payment.amount == Decimal(10)
    assert payment.status == PaymentStatus.PAID
    assert payment.metadata_json["terms_origin"] == "operator_receipt_verified"
    assert await session.scalar(select(func.count(AuditLog.id))) == 1
    assert await session.scalar(select(func.count(AccessOperation.id))) == 1
    assert await session.scalar(select(func.count(PaymentApplication.id))) == 0
    with pytest.raises(ValueError, match="зафиксированы"):
        await PurchaseReconciliationService(session).reconcile(
            actor_id=42,
            payment_id=payment.id,
            channel_id=channel.id,
            duration_days=90,
            reference="Another receipt",
            apply=True,
            confirmed_unapplied=True,
        )


@pytest.mark.asyncio
async def test_reconciliation_never_unmarks_legacy_application(session, monkeypatch):
    payment, channel = await legacy_payment(session, monkeypatch)
    now = datetime(2026, 10, 13)
    subscription = Subscription(
        user_id=payment.user_id,
        channel_id=channel.id,
        payment_id=payment.id,
        starts_at=now,
        expires_at=now + timedelta(days=65),
    )
    session.add(subscription)
    await session.flush()
    application = PaymentApplication(
        payment_id=payment.id,
        subscription_id=subscription.id,
        days_granted=0,
        applied_at=now,
        starts_at=now,
        expires_at=subscription.expires_at,
        legacy=True,
    )
    session.add(application)
    await session.commit()
    with pytest.raises(ValueError, match="связана с доступом"):
        await PurchaseReconciliationService(session).reconcile(
            actor_id=42,
            payment_id=payment.id,
            channel_id=channel.id,
            duration_days=30,
            reference="Synthetic receipt",
            apply=True,
            confirmed_unapplied=True,
        )
    assert application.legacy
    assert payment.purchased_duration_days == 3
    assert subscription.expires_at == now + timedelta(days=65)
    assert await session.scalar(select(func.count(AuditLog.id))) == 0
