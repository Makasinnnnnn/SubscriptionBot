from datetime import UTC, datetime, timedelta

import pytest

from app.bot.handlers.subscriptions import _join_request_is_allowed
from app.db.enums import SubscriptionStatus
from app.db.models import Channel, InviteLink, Subscription, User


@pytest.mark.asyncio
async def test_active_subscriber_can_join_both_channels(session, monkeypatch) -> None:
    materials_chat_id = -1002
    monkeypatch.setattr(
        "app.bot.handlers.subscriptions.get_settings",
        lambda: type("Settings", (), {"materials_channel_id": materials_chat_id})(),
    )
    user = User(telegram_id=500)
    channel = Channel(telegram_chat_id=-1001, title="Deals")
    session.add_all([user, channel])
    await session.flush()
    subscription = Subscription(
        user_id=user.id,
        channel_id=channel.id,
        status=SubscriptionStatus.ACTIVE,
        starts_at=datetime.now(UTC).replace(tzinfo=None),
        expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30),
        source="manual",
    )
    session.add(subscription)
    await session.flush()
    session.add(
        InviteLink(
            subscription_id=subscription.id,
            link="https://t.me/+primary",
            expire_at=subscription.expires_at,
            materials_channel_id=materials_chat_id,
            materials_link="https://t.me/+materials",
            materials_expire_at=subscription.expires_at,
        )
    )
    await session.flush()

    assert await _join_request_is_allowed(
        session,
        telegram_id=500,
        chat_id=-1001,
        invite_value="https://t.me/+primary",
    )
    assert await _join_request_is_allowed(
        session,
        telegram_id=500,
        chat_id=materials_chat_id,
        invite_value="https://t.me/+materials",
    )


@pytest.mark.asyncio
async def test_join_request_rejects_shared_or_expired_access(session, monkeypatch) -> None:
    materials_chat_id = -1002
    monkeypatch.setattr(
        "app.bot.handlers.subscriptions.get_settings",
        lambda: type("Settings", (), {"materials_channel_id": materials_chat_id})(),
    )
    subscriber = User(telegram_id=500)
    outsider = User(telegram_id=501)
    channel = Channel(telegram_chat_id=-1001, title="Deals")
    session.add_all([subscriber, outsider, channel])
    await session.flush()
    subscription = Subscription(
        user_id=subscriber.id,
        channel_id=channel.id,
        status=SubscriptionStatus.EXPIRED,
        starts_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=31),
        expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1),
        source="manual",
    )
    session.add(subscription)
    await session.flush()
    session.add(
        InviteLink(
            subscription_id=subscription.id,
            link="https://t.me/+primary",
            expire_at=subscription.expires_at,
            materials_channel_id=materials_chat_id,
            materials_link="https://t.me/+materials",
            materials_expire_at=subscription.expires_at,
        )
    )
    await session.flush()

    assert not await _join_request_is_allowed(
        session,
        telegram_id=501,
        chat_id=-1001,
        invite_value="https://t.me/+primary",
    )
    assert not await _join_request_is_allowed(
        session,
        telegram_id=500,
        chat_id=-1001,
        invite_value="https://t.me/+primary",
    )
