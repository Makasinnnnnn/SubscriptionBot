from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramRetryAfter
from sqlalchemy import select

from app.db.enums import PaymentStatus
from app.db.models import AccessOperation, Channel, Payment, Plan, Subscription, User, WebhookEvent
from app.payments.base import PaymentStatusResult
from app.payments.providers import WebhookVerificationError
from app.services.webhook_service import WebhookService
from app.subscriptions.service import SubscriptionService


class FakePaidProvider:
    def __init__(
        self,
        amount: Decimal = Decimal(100),
        *,
        provider_payment_id: str = "provider-1",
        currency: str = "RUB",
    ) -> None:
        self.amount = amount
        self.provider_payment_id = provider_payment_id
        self.currency = currency

    async def handle_webhook(self, payload, headers, raw_body):
        return PaymentStatusResult(
            provider_payment_id=self.provider_payment_id,
            status=PaymentStatus.PAID,
            amount=self.amount,
            currency=self.currency,
            raw_payload={"verified": True},
        )


class RejectingProvider:
    async def handle_webhook(self, payload, headers, raw_body):
        raise WebhookVerificationError("forged")


@pytest.fixture(autouse=True)
def disable_real_admin_notifications(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.admin_notification_service.get_settings",
        lambda: SimpleNamespace(
            ops_bot_token="",
            ops_chat_ids=[],
            admin_ids=[],
        ),
    )


async def _payment(session) -> Payment:
    user = User(telegram_id=500)
    channel = Channel(telegram_chat_id=-1001, title="Deals")
    session.add_all([user, channel])
    await session.flush()
    plan = Plan(
        channel_id=channel.id,
        title="30 days",
        duration_days=30,
        price=Decimal(100),
        currency="RUB",
    )
    session.add(plan)
    await session.flush()
    payment = Payment(
        user_id=user.id,
        plan_id=plan.id,
        provider="fake",
        provider_payment_id="provider-1",
        amount=Decimal(100),
        currency="RUB",
        idempotency_key="idem-1",
    )
    session.add(payment)
    await session.commit()
    return payment


@pytest.mark.asyncio
async def test_webhook_retry_after_schedules_delivery_without_losing_payment(session, monkeypatch):
    payment = await _payment(session)
    monkeypatch.setattr(
        "app.services.webhook_service.get_payment_provider", lambda _: FakePaidProvider()
    )
    monkeypatch.setattr(WebhookService, "_bot", staticmethod(lambda: None))

    async def limited(self, payment):
        raise TelegramRetryAfter(method=None, message="synthetic delivery limit", retry_after=120)

    monkeypatch.setattr(WebhookService, "_activate_and_notify", limited)
    result = await WebhookService(session).process(
        "fake", {"update_id": 99, "update_type": "invoice_paid"}, {}, b"synthetic"
    )
    assert result["status"] == "ok"
    operation = await session.scalar(
        select(AccessOperation).where(AccessOperation.payment_id == payment.id)
    )
    assert payment.status == PaymentStatus.PAID
    assert operation.completed_at is None
    assert operation.attempts == 1
    assert operation.next_attempt_at > datetime.now(UTC).replace(tzinfo=None) + timedelta(
        seconds=115
    )


@pytest.mark.asyncio
async def test_successful_webhook_activates_once(session, monkeypatch) -> None:
    payment = await _payment(session)
    provider = FakePaidProvider()
    monkeypatch.setattr("app.services.webhook_service.get_payment_provider", lambda name: provider)
    monkeypatch.setattr(WebhookService, "_bot", staticmethod(lambda: None))
    service = WebhookService(session)
    payload = {"update_id": 1, "update_type": "invoice_paid"}

    first = await service.process("fake", payload, {}, b"first")
    second = await service.process("fake", payload, {}, b"first")

    subscriptions = list(await session.scalars(Subscription.__table__.select()))
    assert first == {"status": "ok"}
    assert second == {"status": "duplicate"}
    assert payment.status == PaymentStatus.PAID
    assert len(subscriptions) == 1


@pytest.mark.asyncio
async def test_paid_webhook_rolls_back_partial_subscription_on_provision_failure(
    session,
    monkeypatch,
) -> None:
    payment = await _payment(session)
    provider = FakePaidProvider()
    monkeypatch.setattr(
        "app.services.webhook_service.get_payment_provider",
        lambda _name: provider,
    )
    monkeypatch.setattr(WebhookService, "_bot", staticmethod(lambda: None))

    async def fail_after_flush(self, target_payment, bot):
        now = datetime.now(UTC).replace(tzinfo=None)
        plan = await self.session.get(Plan, target_payment.plan_id)
        self.session.add(
            Subscription(
                user_id=target_payment.user_id,
                channel_id=plan.channel_id,
                payment_id=target_payment.id,
                starts_at=now,
                period_started_at=now,
                expires_at=now + timedelta(days=30),
            )
        )
        await self.session.flush()
        raise ValueError("telegram provisioning failed")

    monkeypatch.setattr(
        SubscriptionService,
        "activate_from_payment",
        fail_after_flush,
    )

    result = await WebhookService(session).process(
        "fake",
        {"update_id": 7},
        {},
        b"partial",
    )

    assert result == {"status": "ok"}
    await session.refresh(payment)
    assert payment.status == PaymentStatus.PAID
    assert list(await session.scalars(Subscription.__table__.select())) == []
    event = await session.scalar(select(WebhookEvent).order_by(WebhookEvent.id.desc()).limit(1))
    assert event.error == "access_provision_failed"


@pytest.mark.asyncio
async def test_webhook_amount_mismatch_is_rejected(session, monkeypatch) -> None:
    payment = await _payment(session)
    provider = FakePaidProvider(Decimal(101))
    monkeypatch.setattr("app.services.webhook_service.get_payment_provider", lambda name: provider)
    service = WebhookService(session)

    result = await service.process("fake", {"update_id": 2}, {}, b"second")

    assert result == {"status": "rejected"}
    assert payment.status == PaymentStatus.PENDING


@pytest.mark.asyncio
async def test_unverified_webhook_is_not_persisted(session, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.webhook_service.get_payment_provider",
        lambda name: RejectingProvider(),
    )

    with pytest.raises(WebhookVerificationError):
        await WebhookService(session).process("fake", {"id": 1}, {}, b"forged")

    assert list(await session.scalars(WebhookEvent.__table__.select())) == []


@pytest.mark.asyncio
async def test_unknown_invoice_and_currency_mismatch_do_not_activate(session, monkeypatch) -> None:
    payment = await _payment(session)
    service = WebhookService(session)

    monkeypatch.setattr(
        "app.services.webhook_service.get_payment_provider",
        lambda name: FakePaidProvider(provider_payment_id="missing"),
    )
    unknown = await service.process("fake", {"id": "unknown"}, {}, b"unknown")
    assert unknown == {"status": "payment_not_found"}

    monkeypatch.setattr(
        "app.services.webhook_service.get_payment_provider",
        lambda name: FakePaidProvider(currency="USD"),
    )
    mismatch = await service.process("fake", {"id": "currency"}, {}, b"currency")
    assert mismatch == {"status": "rejected"}
    assert payment.status == PaymentStatus.PENDING
    assert list(await session.scalars(Subscription.__table__.select())) == []


@pytest.mark.asyncio
async def test_refunded_payment_cannot_be_reactivated(session, monkeypatch) -> None:
    payment = await _payment(session)
    payment.status = PaymentStatus.REFUNDED
    await session.commit()
    monkeypatch.setattr(
        "app.services.webhook_service.get_payment_provider",
        lambda name: FakePaidProvider(),
    )

    result = await WebhookService(session).process("fake", {"id": "refunded"}, {}, b"refunded")

    assert result == {"status": "rejected"}
    assert payment.status == PaymentStatus.REFUNDED
    assert list(await session.scalars(Subscription.__table__.select())) == []


def test_cryptobot_event_key_does_not_rely_on_non_unique_update_id() -> None:
    first = WebhookService._event_id(
        "cryptobot",
        {
            "update_id": 1,
            "update_type": "invoice_paid",
            "payload": {"invoice_id": 10, "status": "paid"},
        },
        b"first",
    )
    second = WebhookService._event_id(
        "cryptobot",
        {
            "update_id": 1,
            "update_type": "invoice_paid",
            "payload": {"invoice_id": 11, "status": "paid"},
        },
        b"second",
    )
    assert first != second
