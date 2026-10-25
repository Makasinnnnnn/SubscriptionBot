from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import RefundedPayment, SuccessfulPayment
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.enums import PaymentStatus, SubscriptionStatus
from app.db.models import AccessOperation, Channel, InviteLink, Payment, Plan, Subscription, User
from app.services.access_operation_service import AccessOperationService
from app.subscriptions.service import SubscriptionService


class RecordingBot:
    def __init__(self, fail_chat=None):
        self.fail_chat = fail_chat
        self.bans = []

    async def ban_chat_member(self, chat_id, user_id):
        self.bans.append((chat_id, user_id))
        if chat_id == self.fail_chat:
            raise TelegramBadRequest(method=None, message="test ban failure")

    async def unban_chat_member(self, **kwargs):
        pass

    async def revoke_chat_invite_link(self, **kwargs):
        pass

    async def send_message(self, *args, **kwargs):
        pass

    async def get_me(self):
        return SimpleNamespace(id=999)

    async def get_chat_member(self, *args, **kwargs):
        return SimpleNamespace(
            status=ChatMemberStatus.ADMINISTRATOR, can_invite_users=True, can_restrict_members=True
        )

    async def create_chat_invite_link(self, chat_id, **kwargs):
        if chat_id == self.fail_chat:
            raise TelegramBadRequest(method=None, message="test creation failure")
        return SimpleNamespace(invite_link=f"https://t.me/+audit-{chat_id}")


async def seed(session, monkeypatch, *, second=False):
    monkeypatch.setattr(
        "app.subscriptions.service.get_settings", lambda: SimpleNamespace(materials_channel_id=None)
    )

    async def notify(*args, **kwargs):
        pass

    monkeypatch.setattr(SubscriptionService, "_notify_admins", notify)
    now = datetime.now(UTC).replace(tzinfo=None)
    user = User(telegram_id=500)
    channels = [
        Channel(telegram_chat_id=-1001, title="A"),
        Channel(telegram_chat_id=-1002, title="B"),
    ]
    session.add_all([user, *channels])
    await session.flush()
    subscriptions = []
    for index, channel in enumerate(channels[: 2 if second else 1]):
        sub = Subscription(
            user_id=user.id,
            channel_id=channel.id,
            starts_at=now - timedelta(days=2),
            expires_at=now + timedelta(days=10),
            source="manual",
        )
        session.add(sub)
        await session.flush()
        session.add(
            InviteLink(
                subscription_id=sub.id,
                link=f"https://t.me/+main{index}",
                materials_channel_id=-1099,
                materials_link=f"https://t.me/+materials{index}",
            )
        )
        subscriptions.append(sub)
    await session.commit()
    return subscriptions


@pytest.mark.asyncio
async def test_failed_revoke_survives_new_session(session, monkeypatch):
    (sub,) = await seed(session, monkeypatch)
    result = await SubscriptionService(session).revoke(sub.id, RecordingBot(-1001))
    assert result.error
    await session.commit()
    bot = RecordingBot()
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as fresh:
        pending = await fresh.scalar(
            select(AccessOperation).where(AccessOperation.completed_at.is_(None))
        )
        assert pending is not None
        pending.next_attempt_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        await fresh.commit()
        assert await AccessOperationService(fresh).process_pending(bot) == 1
    assert bot.bans == [(-1001, 500)]


@pytest.mark.asyncio
async def test_shared_entitlement_prevents_removal(session, monkeypatch):
    a, b = await seed(session, monkeypatch, second=True)
    bot = RecordingBot()
    await SubscriptionService(session).revoke(a.id, bot)
    await session.commit()
    assert b.status == SubscriptionStatus.ACTIVE
    assert (-1099, 500) not in bot.bans


@pytest.mark.asyncio
async def test_secondary_failure_is_reported(session, monkeypatch):
    (sub,) = await seed(session, monkeypatch)
    result = await SubscriptionService(session).revoke(sub.id, RecordingBot(-1099))
    assert result.error == "TelegramBadRequest"
    assert not result.telegram_access_changed


@pytest.mark.asyncio
async def test_stale_revoke_skips_resumed_entitlement(session, monkeypatch):
    (sub,) = await seed(session, monkeypatch)
    await SubscriptionService(session).revoke(sub.id, RecordingBot(-1001))
    sub.status = SubscriptionStatus.ACTIVE
    sub.expires_at += timedelta(days=3)
    pending = await session.scalar(
        select(AccessOperation).where(AccessOperation.completed_at.is_(None))
    )
    pending.next_attempt_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    await session.commit()
    bot = RecordingBot()
    await AccessOperationService(session).process_pending(bot)
    assert bot.bans == []


@pytest.mark.asyncio
async def test_no_bot_does_not_claim_jobs(session):
    operation = await AccessOperationService(session).enqueue(
        "cleanup:test", "cleanup_link", chat_id=-1001, invite_link="https://t.me/+old"
    )
    await session.commit()
    original = operation.next_attempt_at
    assert await AccessOperationService(session).process_pending(None) == 0
    assert operation.next_attempt_at == original


async def paid_invoice(session, monkeypatch, status=PaymentStatus.PAID):
    await seed(session, monkeypatch)
    user = await session.scalar(select(User))
    channel = await session.scalar(select(Channel).where(Channel.telegram_chat_id == -1001))
    plan = Plan(
        channel_id=channel.id, title="Month", duration_days=30, price=Decimal(30), currency="XTR"
    )
    session.add(plan)
    await session.flush()
    payment = Payment(
        user_id=user.id,
        plan_id=plan.id,
        provider="telegram-stars",
        amount=Decimal(30),
        currency="XTR",
        status=status,
        idempotency_key="test-durable-payment",
    )
    session.add(payment)
    await session.commit()
    return payment


@pytest.mark.asyncio
async def test_provision_savepoint_failure_retried_after_restart(session, monkeypatch):
    payment = await paid_invoice(session, monkeypatch)
    operation = await AccessOperationService(session).enqueue_payment(payment.id)
    await session.commit()
    assert not await AccessOperationService(session).execute(operation, RecordingBot(-1001))
    await session.commit()
    assert operation.last_error == "TelegramBadRequest"
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as fresh:
        pending = await fresh.scalar(
            select(AccessOperation).where(AccessOperation.id == operation.id)
        )
        pending.next_attempt_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        await fresh.commit()
        assert await AccessOperationService(fresh).process_pending(RecordingBot()) == 1
        assert pending.completed_at is not None


@pytest.mark.asyncio
async def test_failed_second_link_compensation_is_durable(session, monkeypatch):
    payment = await paid_invoice(session, monkeypatch)
    monkeypatch.setattr(
        "app.subscriptions.service.get_settings",
        lambda: SimpleNamespace(materials_channel_id=-1099),
    )

    class BrokenSecond(RecordingBot):
        async def revoke_chat_invite_link(self, **kwargs):
            raise TelegramBadRequest(method=None, message="test compensation failure")

    operation = await AccessOperationService(session).enqueue_payment(payment.id)
    await session.commit()
    assert not await AccessOperationService(session).execute(operation, BrokenSecond(-1099))
    await session.commit()
    cleanup = await session.scalar(
        select(AccessOperation).where(AccessOperation.kind == "cleanup_link")
    )
    assert cleanup is not None
    assert cleanup.chat_id == -1001
    assert cleanup.invite_link == "https://t.me/+audit--1001"
    assert cleanup.subscription_id is None


@pytest.mark.asyncio
async def test_failed_old_link_rotation_enqueues_cleanup(session, monkeypatch):
    await seed(session, monkeypatch)

    class BrokenRevoke(RecordingBot):
        async def revoke_chat_invite_link(self, **kwargs):
            raise TelegramBadRequest(method=None, message="test old link failure")

    assert not await SubscriptionService(session)._revoke_invite_safely(
        BrokenRevoke(), -1001, "https://t.me/+old", 1, "primary"
    )
    await session.commit()
    cleanup = await session.scalar(
        select(AccessOperation).where(AccessOperation.kind == "cleanup_link")
    )
    assert cleanup.invite_link == "https://t.me/+old"


@pytest.mark.asyncio
async def test_already_revoked_link_acknowledges_retry(session):
    class AlreadyRevoked(RecordingBot):
        async def revoke_chat_invite_link(self, **kwargs):
            raise TelegramBadRequest(method=None, message="invite link is already revoked")

    operation = await AccessOperationService(session).enqueue(
        "cleanup:already", "cleanup_link", chat_id=-1001, invite_link="https://t.me/+old"
    )
    await session.commit()
    assert await AccessOperationService(session).execute(operation, AlreadyRevoked())
    assert operation.completed_at is not None
    assert operation.last_error is None


@pytest.mark.asyncio
async def test_late_stars_and_refund_event_use_real_models(session, monkeypatch):
    from app.bot.handlers import subscriptions as handlers

    payment = await paid_invoice(session, monkeypatch, PaymentStatus.EXPIRED)

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            return None

    class Notifications:
        def __init__(self, *args):
            pass

        async def notify(self, *args):
            pass

    async def answer(*args, **kwargs):
        pass

    monkeypatch.setattr(handlers, "async_session_factory", Context)
    monkeypatch.setattr(handlers, "AdminNotificationService", Notifications)
    charge = SuccessfulPayment(
        currency="XTR",
        total_amount=30,
        invoice_payload=f"payment:{payment.id}",
        telegram_payment_charge_id="charge-audit",
        provider_payment_charge_id="",
    )
    message = SimpleNamespace(
        successful_payment=charge,
        from_user=SimpleNamespace(id=500),
        bot=RecordingBot(),
        answer=answer,
    )
    await handlers.telegram_stars_successful_payment(message)
    assert payment.status == PaymentStatus.PAID
    refund = RefundedPayment(
        currency="XTR",
        total_amount=30,
        invoice_payload=f"payment:{payment.id}",
        telegram_payment_charge_id="charge-audit",
        provider_payment_charge_id="",
    )
    message.refunded_payment = refund
    await handlers.telegram_stars_refunded_payment(message)
    assert payment.status == PaymentStatus.REFUNDED
    await handlers.telegram_stars_refunded_payment(message)
    assert payment.status == PaymentStatus.REFUNDED


def test_production_checkout_lists_enabled_providers_without_sbp(monkeypatch):
    from app.bot.keyboards import subscription as keyboards

    settings = SimpleNamespace(
        app_env="production",
        enable_telegram_stars=True,
        enable_cryptobot=True,
        cryptobot_token="synthetic",
        enable_yookassa=True,
        yookassa_shop_id="synthetic",
        yookassa_secret_key="synthetic",
    )
    monkeypatch.setattr(keyboards, "get_settings", lambda: settings)
    plan = SimpleNamespace(id=1, stars_price=30, currency="USD")
    markup = keyboards.payment_providers_keyboard(plan)
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "pay:1:telegram-stars" in callbacks
    assert "pay:1:cryptobot" in callbacks
    assert not any("yookassa" in c or "sbp" in c for c in callbacks)
