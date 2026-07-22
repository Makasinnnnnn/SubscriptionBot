from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from decimal import Decimal

from app.db.enums import PaymentStatus, SubscriptionStatus
from app.db.models import Channel, Payment, Plan, Subscription, User
from app.subscriptions.service import SubscriptionService


class FakeBot:
    def __init__(self) -> None:
        self.banned: list[tuple[int, int]] = []
        self.unbanned: list[tuple[int, int]] = []
        self.messages: list[tuple[int, str]] = []
        self.invites: list[tuple[int, str, int]] = []
        self.revoked_invites: list[tuple[int, str]] = []

    async def create_chat_invite_link(self, chat_id, name, expire_date, member_limit):
        self.invites.append((chat_id, name, member_limit))
        return SimpleNamespace(invite_link=f"https://t.me/+{name}")

    async def ban_chat_member(self, chat_id, user_id):
        self.banned.append((chat_id, user_id))

    async def unban_chat_member(self, chat_id, user_id, only_if_banned):
        self.unbanned.append((chat_id, user_id))

    async def revoke_chat_invite_link(self, chat_id, invite_link):
        self.revoked_invites.append((chat_id, invite_link))

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


@pytest.mark.asyncio
async def test_manual_grant_extends_existing_subscription(session) -> None:
    channel = Channel(telegram_chat_id=-1001, title="Deals")
    session.add(channel)
    await session.flush()
    bot = FakeBot()
    service = SubscriptionService(session)

    first, invite = await service.grant_manual(500, channel.id, 10, bot)
    first_expiry = first.expires_at
    second, _ = await service.grant_manual(500, channel.id, 5, bot)
    await session.commit()

    assert first.id == second.id
    assert second.source == "manual"
    assert second.expires_at == first_expiry + timedelta(days=5)
    assert invite.link.startswith("https://t.me/+")


@pytest.mark.asyncio
async def test_manual_revoke_removes_member_and_marks_cancelled(session) -> None:
    channel = Channel(telegram_chat_id=-1001, title="Deals")
    session.add(channel)
    await session.flush()
    bot = FakeBot()
    service = SubscriptionService(session)
    subscription, _ = await service.grant_manual(500, channel.id, 10, bot)

    result = await service.revoke(subscription.id, bot)
    await session.commit()

    assert result.telegram_access_changed
    assert subscription.status == SubscriptionStatus.CANCELLED
    assert bot.banned == [(-1001, 500)]
    assert bot.unbanned == [(-1001, 500)]


@pytest.mark.asyncio
async def test_expiration_removes_member_and_notifies(session) -> None:
    channel = Channel(telegram_chat_id=-1001, title="Deals")
    user = User(telegram_id=500)
    session.add_all([channel, user])
    await session.flush()
    subscription = Subscription(
        user_id=user.id,
        channel_id=channel.id,
        status=SubscriptionStatus.ACTIVE,
        starts_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2),
        expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1),
        source="manual",
    )
    session.add(subscription)
    await session.commit()

    bot = FakeBot()
    count = await SubscriptionService(session).expire_due_subscriptions(bot)

    assert count == 1
    assert subscription.status == SubscriptionStatus.EXPIRED
    assert bot.banned == [(-1001, 500)]
    user_messages = [text for telegram_id, text in bot.messages if telegram_id == 500]
    assert len(user_messages) == 1


@pytest.mark.asyncio
async def test_paid_subscription_uses_and_revokes_both_channels(
    session,
    monkeypatch,
) -> None:
    materials_chat_id = -1002
    monkeypatch.setattr(
        "app.subscriptions.service.get_settings",
        lambda: SimpleNamespace(materials_channel_id=materials_chat_id),
    )
    monkeypatch.setattr(
        "app.services.admin_notification_service.get_settings",
        lambda: SimpleNamespace(ops_bot_token="", ops_chat_ids=[], admin_ids=[]),
    )
    user = User(telegram_id=500)
    channel = Channel(telegram_chat_id=-1001, title="Deals")
    session.add_all([user, channel])
    await session.flush()
    plan = Plan(
        channel_id=channel.id,
        title="Month",
        duration_days=30,
        price=Decimal("4990"),
        currency="RUB",
    )
    session.add(plan)
    await session.flush()
    payment = Payment(
        user_id=user.id,
        plan_id=plan.id,
        provider="cryptobot",
        provider_payment_id="invoice-1",
        status=PaymentStatus.PAID,
        amount=Decimal("4990"),
        currency="RUB",
        idempotency_key="payment-1",
    )
    session.add(payment)
    await session.flush()
    bot = FakeBot()
    service = SubscriptionService(session)

    subscription, primary, materials = await service.activate_from_payment(payment, bot)
    first_expiry = subscription.expires_at
    repeated, repeated_primary, repeated_materials = await service.activate_from_payment(
        payment, bot
    )
    await session.commit()

    assert repeated.id == subscription.id
    assert repeated.expires_at == first_expiry
    assert repeated_primary.link == primary.link
    assert repeated_materials == materials
    assert bot.invites == [
        (-1001, f"sub-{subscription.id}", 1),
        (materials_chat_id, f"materials-sub-{subscription.id}", 1),
    ]
    assert primary.materials_link == materials
    assert primary.materials_channel_id == materials_chat_id

    await service.revoke(subscription.id, bot, reason="test revoke")
    await session.commit()

    assert (-1001, 500) in bot.banned
    assert (materials_chat_id, 500) in bot.banned
    assert (-1001, primary.link) in bot.revoked_invites
    assert (materials_chat_id, materials) in bot.revoked_invites
    assert primary.is_revoked
    assert primary.materials_is_revoked
