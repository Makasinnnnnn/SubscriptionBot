from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import Chat, ChatInviteLink, ChatJoinRequest, Update
from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_access_operations import RecordingBot, paid_invoice

from app.bot.handlers import subscriptions as handlers
from app.db.enums import SubscriptionStatus
from app.db.models import AccessOperation, Channel, InviteLink, Subscription, User
from app.services.access_operation_service import AccessOperationService
from app.subscriptions.service import SubscriptionService
from app.tasks import scheduler


def checkout_settings():
    return SimpleNamespace(
        app_env="production",
        enable_telegram_stars=True,
        enable_cryptobot=True,
        cryptobot_token="synthetic",
        enable_yookassa=True,
        yookassa_shop_id="synthetic",
        yookassa_secret_key="synthetic",
    )


def test_production_tariff_displays_actual_stars_price(monkeypatch):
    from app.bot.keyboards import subscription as keyboards

    monkeypatch.setattr(keyboards, "get_settings", checkout_settings)
    plan = SimpleNamespace(
        id=1,
        title="Month",
        channel=SimpleNamespace(title="A"),
        price=Decimal(4990),
        currency="RUB",
        stars_price=100,
    )
    markup = keyboards.plans_keyboard([plan])
    assert any("100 XTR" in button.text for row in markup.inline_keyboard for button in row)


def test_unconfigured_checkout_offers_support(monkeypatch):
    from app.bot.keyboards import subscription as keyboards

    settings = checkout_settings()
    settings.enable_cryptobot = False
    monkeypatch.setattr(keyboards, "get_settings", lambda: settings)
    plan = SimpleNamespace(id=1, currency="RUB", stars_price=None)
    markup = keyboards.payment_providers_keyboard(plan)
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert "menu:help" in callbacks
    assert not any(value.startswith("pay:") for value in callbacks)


async def seed_access(session, *, minutes=10):
    now = datetime.now(UTC).replace(tzinfo=None)
    user, channel = User(telegram_id=500), Channel(telegram_chat_id=-1001, title="A")
    session.add_all([user, channel])
    await session.flush()
    subscription = Subscription(
        user_id=user.id,
        channel_id=channel.id,
        starts_at=now - timedelta(days=2),
        period_started_at=now - timedelta(days=2),
        expires_at=now + timedelta(minutes=minutes),
        source="manual",
    )
    session.add(subscription)
    await session.flush()
    session.add(
        InviteLink(
            subscription_id=subscription.id,
            link="https://t.me/+verification",
            primary_join_request_enabled=True,
        )
    )
    await session.commit()
    return subscription


@pytest.mark.asyncio
async def test_join_approval_happens_before_lock_context_releases(session, monkeypatch):
    subscription = await seed_access(session)
    observed = []

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            subscription.status = SubscriptionStatus.CANCELLED
            await session.commit()

    class Bot:
        async def approve_chat_join_request(self, **kwargs):
            observed.append(subscription.status)

    monkeypatch.setattr(handlers, "async_session_factory", Context)
    request = SimpleNamespace(
        invite_link=SimpleNamespace(invite_link="https://t.me/+verification"),
        from_user=SimpleNamespace(id=500),
        chat=SimpleNamespace(id=-1001),
        bot=Bot(),
    )
    await handlers.handle_paid_join_request(request)
    assert observed == [SubscriptionStatus.ACTIVE]


@pytest.mark.asyncio
async def test_scheduler_retries_outbox_independently_of_expiry_interval(monkeypatch):
    calls = []

    class Context:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *args):
            pass

    class Runtime:
        def __init__(self, session):
            pass

        async def scheduler_interval_minutes(self, default):
            return 60

    class Operations:
        def __init__(self, session):
            pass

        async def process_pending(self, bot, **kwargs):
            calls.append("queue")
            return 0

    monkeypatch.setattr(scheduler, "async_session_factory", Context)
    monkeypatch.setattr(scheduler, "RuntimeSettingsService", Runtime)
    monkeypatch.setattr(scheduler, "AccessOperationService", Operations)
    monkeypatch.setattr(
        scheduler, "get_settings", lambda: SimpleNamespace(scheduler_interval_minutes=60)
    )
    monkeypatch.setattr(scheduler, "_last_run", datetime.now(UTC))
    await scheduler.expire_subscriptions_job(object())
    assert calls == ["queue"]


@pytest.mark.asyncio
async def test_reminder_after_downtime_uses_actual_expiry(session):
    subscription = await seed_access(session, minutes=2)
    texts = []

    class Bot:
        async def send_message(self, telegram_id, text):
            texts.append(text)

    assert await SubscriptionService(session).send_expiration_reminders(Bot(), [720]) == 1
    assert "12 часов" not in texts[0]
    assert subscription.expires_at.strftime("%d.%m.%Y %H:%M") in texts[0]
    assert "UTC" in texts[0]


@pytest.mark.asyncio
async def test_obsolete_provision_notice_stops_retrying(session, monkeypatch):
    payment = await paid_invoice(session, monkeypatch)
    subscription, *_ = await SubscriptionService(session).activate_from_payment(
        payment, RecordingBot()
    )
    subscription.status = SubscriptionStatus.EXPIRED
    subscription.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
    operation = await AccessOperationService(session).enqueue_payment(payment.id)
    await session.commit()
    assert await AccessOperationService(session).execute(operation, RecordingBot())
    assert operation.completed_at is not None
    assert operation.last_error == "entitlement_inactive"


@pytest.mark.asyncio
async def test_queue_time_budget_preserves_timed_out_operation(session):
    import asyncio

    operation = await AccessOperationService(session).enqueue(
        "verify:timeout", "cleanup_link", chat_id=-1001, invite_link="https://t.me/+old"
    )
    await session.commit()

    class BlockedBot:
        async def revoke_chat_invite_link(self, **kwargs):
            await asyncio.Event().wait()

    assert (
        await asyncio.wait_for(
            AccessOperationService(session).process_pending(BlockedBot(), time_budget_seconds=0.05),
            timeout=1,
        )
        == 0
    )
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as fresh:
        operation = await fresh.get(AccessOperation, operation.id)
        assert operation.completed_at is None
        assert operation.last_error == "TimeoutError"
        assert operation.next_attempt_at > datetime.now(UTC).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_deferred_expiry_notice_retry_honors_retry_after(session, monkeypatch):
    from test_access_operations import seed

    (sub,) = await seed(session, monkeypatch)
    sub.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
    await session.commit()

    class LimitedBot(RecordingBot):
        async def send_message(self, *args, **kwargs):
            raise TelegramRetryAfter(method=None, message="test notice limit", retry_after=120)

    await SubscriptionService(session).expire_due_subscriptions(LimitedBot(), defer_telegram=True)
    await AccessOperationService(session).process_pending(LimitedBot())
    operation = await session.scalar(
        select(AccessOperation).where(AccessOperation.kind == "expiry_notice")
    )
    assert operation.completed_at is None
    assert operation.last_error == "TelegramRetryAfter"
    assert operation.next_attempt_at > datetime.now(UTC).replace(tzinfo=None) + timedelta(
        seconds=100
    )


@pytest.mark.asyncio
async def test_membership_revocation_precedes_older_cleanup_backlog(session):
    sub = await seed_access(session)
    sub.status = SubscriptionStatus.CANCELLED
    queue = AccessOperationService(session)
    await queue.enqueue(
        "verify:older-cleanup", "cleanup_link", chat_id=-1001, invite_link="https://t.me/+old"
    )
    await queue.enqueue(
        "verify:urgent-removal",
        "remove_member",
        subscription_id=sub.id,
        telegram_id=500,
        chat_id=-1001,
        expires_at_snapshot=sub.expires_at,
    )
    await session.commit()
    bot = RecordingBot()
    assert await queue.process_pending(bot, limit=1) == 1
    assert bot.bans == [(-1001, 500)]


@pytest.mark.asyncio
async def test_dispatcher_distinguishes_join_updates_in_same_second(session, monkeypatch):
    await seed_access(session)

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(handlers, "async_session_factory", Context)
    bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDE")
    approve = AsyncMock()
    monkeypatch.setattr(bot, "approve_chat_join_request", approve)
    dispatcher = Dispatcher()
    router = Router()
    router.chat_join_request.register(handlers.handle_paid_join_request)
    dispatcher.include_router(router)
    request = ChatJoinRequest(
        chat=Chat(id=-1001, type="channel"),
        from_user=TelegramUser(id=500, is_bot=False, first_name="Synthetic"),
        user_chat_id=500,
        date=datetime.now(UTC).replace(microsecond=0),
        invite_link=ChatInviteLink(
            invite_link="https://t.me/+verification",
            creator=TelegramUser(id=999, is_bot=True, first_name="Bot"),
            creates_join_request=True,
            is_primary=False,
            is_revoked=False,
        ),
    )
    try:
        await dispatcher.feed_update(bot, Update(update_id=1, chat_join_request=request))
        await dispatcher.feed_update(bot, Update(update_id=1, chat_join_request=request))
        assert approve.await_count == 1
        await dispatcher.feed_update(bot, Update(update_id=2, chat_join_request=request))
        assert approve.await_count == 2
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_wrapped_telegram_rate_limit_preserves_wait(session, monkeypatch):
    payment = await paid_invoice(session, monkeypatch)
    operation = await AccessOperationService(session).enqueue_payment(payment.id)
    await session.commit()

    class LimitedPermissions(RecordingBot):
        async def get_chat_member(self, *args, **kwargs):
            raise TelegramRetryAfter(method=None, message="test permissions limit", retry_after=120)

    assert not await AccessOperationService(session).execute(operation, LimitedPermissions())
    assert operation.next_attempt_at > datetime.now(UTC).replace(tzinfo=None) + timedelta(
        seconds=100
    )


@pytest.mark.asyncio
async def test_membership_confirmation_rate_limit_preserves_wait(session):
    sub = await seed_access(session)
    operation = await AccessOperationService(session).enqueue_join(
        500, -1001, "https://t.me/+verification", datetime.now(UTC), sub.id
    )
    await session.commit()

    class LimitedMembership:
        async def approve_chat_join_request(self, **kwargs):
            raise TelegramBadRequest(method=None, message="USER_ALREADY_PARTICIPANT")

        async def get_chat_member(self, *args):
            raise TelegramRetryAfter(method=None, message="test membership limit", retry_after=120)

    assert not await AccessOperationService(session).execute(operation, LimitedMembership())
    assert operation.completed_at is None
    assert operation.next_attempt_at > datetime.now(UTC).replace(tzinfo=None) + timedelta(
        seconds=100
    )


@pytest.mark.asyncio
async def test_webhook_provision_rate_limit_is_saved_before_commit(session, monkeypatch):
    from test_webhook_service import FakePaidProvider, _payment

    from app.services.webhook_service import WebhookService

    payment = await _payment(session)
    monkeypatch.setattr(
        "app.services.webhook_service.get_payment_provider", lambda name: FakePaidProvider()
    )
    monkeypatch.setattr(WebhookService, "_bot", staticmethod(lambda: None))

    async def limited(*args):
        raise TelegramRetryAfter(method=None, message="test immediate rate limit", retry_after=120)

    monkeypatch.setattr(WebhookService, "_activate_and_notify", limited)
    assert await WebhookService(session).process(
        "fake", {"id": "verification-rate"}, {}, b"synthetic"
    ) == {"status": "ok"}
    operation = await session.scalar(
        select(AccessOperation).where(AccessOperation.payment_id == payment.id)
    )
    assert operation.completed_at is None
    assert operation.next_attempt_at > datetime.now(UTC).replace(tzinfo=None) + timedelta(
        seconds=100
    )
