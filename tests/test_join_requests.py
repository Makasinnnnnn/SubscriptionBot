from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import (
    Chat,
    ChatInviteLink,
    ChatJoinRequest,
    Update,
    User as TelegramUser,
)

from app.bot.handlers import subscriptions as subscription_handlers
from app.bot.runner import polling_update_types
from app.db.enums import SubscriptionStatus
from app.db.models import Channel, InviteLink, Subscription, User
from app.subscriptions.join_requests import JoinRequestService


async def _seed_access(
    session,
    *,
    telegram_id: int = 500,
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
    expires_delta: timedelta = timedelta(days=30),
    revoked: bool = False,
    join_request_enabled: bool = True,
) -> tuple[Subscription, InviteLink]:
    user = User(telegram_id=telegram_id)
    channel = Channel(telegram_chat_id=-1001, title="Deals")
    session.add_all([user, channel])
    await session.flush()
    now = datetime.now(UTC).replace(tzinfo=None)
    subscription = Subscription(
        user_id=user.id,
        channel_id=channel.id,
        status=status,
        starts_at=now - timedelta(days=1),
        expires_at=now + expires_delta,
        revoked_at=now if revoked else None,
        source="manual",
    )
    session.add(subscription)
    await session.flush()
    invite = InviteLink(
        subscription_id=subscription.id,
        link="https://t.me/+primary",
        expire_at=now + timedelta(days=1),
        primary_join_request_enabled=join_request_enabled,
        materials_channel_id=-1002,
        materials_link="https://t.me/+materials",
        materials_expire_at=now + timedelta(days=1),
        materials_join_request_enabled=join_request_enabled,
    )
    session.add(invite)
    await session.flush()
    return subscription, invite


@pytest.mark.asyncio
async def test_active_subscriber_can_join_and_rejoin_both_channels(session) -> None:
    await _seed_access(session)
    service = JoinRequestService(session)

    for _ in range(2):
        primary = await service.decide(
            telegram_id=500,
            chat_id=-1001,
            invite_value="https://t.me/+primary",
        )
        materials = await service.decide(
            telegram_id=500,
            chat_id=-1002,
            invite_value="https://t.me/+materials",
        )
        assert primary.allowed and primary.access_kind == "primary"
        assert materials.allowed and materials.access_kind == "materials"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expires_delta", "revoked", "reason"),
    [
        (SubscriptionStatus.EXPIRED, timedelta(days=-1), False, "subscription_expired"),
        (SubscriptionStatus.ACTIVE, timedelta(days=-1), False, "subscription_expired"),
        (SubscriptionStatus.CANCELLED, timedelta(days=10), False, "subscription_cancelled"),
        (SubscriptionStatus.ACTIVE, timedelta(days=10), True, "subscription_revoked"),
    ],
)
async def test_inactive_subscription_is_declined(
    session,
    status,
    expires_delta,
    revoked,
    reason,
) -> None:
    await _seed_access(
        session,
        status=status,
        expires_delta=expires_delta,
        revoked=revoked,
    )

    decision = await JoinRequestService(session).decide(
        telegram_id=500,
        chat_id=-1001,
        invite_value="https://t.me/+primary",
    )

    assert not decision.allowed
    assert decision.reason == reason


@pytest.mark.asyncio
async def test_legacy_link_is_declined(session) -> None:
    await _seed_access(session, join_request_enabled=False)

    decision = await JoinRequestService(session).decide(
        telegram_id=500,
        chat_id=-1001,
        invite_value="https://t.me/+primary",
    )

    assert not decision.allowed
    assert decision.reason == "legacy_link"


@pytest.mark.asyncio
async def test_shared_link_is_declined_for_other_account(session) -> None:
    await _seed_access(session)
    session.add(User(telegram_id=501))
    await session.flush()

    decision = await JoinRequestService(session).decide(
        telegram_id=501,
        chat_id=-1001,
        invite_value="https://t.me/+primary",
    )

    assert not decision.allowed
    assert decision.reason == "subscription_missing"


@pytest.mark.asyncio
async def test_dispatcher_approves_real_join_request_update(
    session,
    monkeypatch,
) -> None:
    await _seed_access(session)

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(
        subscription_handlers,
        "async_session_factory",
        SessionContext,
    )
    bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDE")
    approve = AsyncMock()
    decline = AsyncMock()
    monkeypatch.setattr(bot, "approve_chat_join_request", approve)
    monkeypatch.setattr(bot, "decline_chat_join_request", decline)
    dispatcher = Dispatcher()
    dispatcher.include_router(subscription_handlers.router)

    try:
        assert "chat_join_request" in polling_update_types(dispatcher)
        for update_id, (chat_id, invite_value) in enumerate(
            (
                (-1001, "https://t.me/+primary"),
                (-1002, "https://t.me/+materials"),
            ),
            start=1,
        ):
            telegram_user = TelegramUser(
                id=500,
                is_bot=False,
                first_name="Subscriber",
            )
            invite = ChatInviteLink(
                invite_link=invite_value,
                creator=TelegramUser(id=999, is_bot=True, first_name="PropGate"),
                creates_join_request=True,
                is_primary=False,
                is_revoked=False,
            )
            update = Update(
                update_id=update_id,
                chat_join_request=ChatJoinRequest(
                    chat=Chat(id=chat_id, type="channel", title="Private"),
                    from_user=telegram_user,
                    user_chat_id=500,
                    date=datetime.now(UTC),
                    invite_link=invite,
                ),
            )
            await dispatcher.feed_update(bot, update)
        outsider_update = Update(
            update_id=3,
            chat_join_request=ChatJoinRequest(
                chat=Chat(id=-1001, type="channel", title="Private"),
                from_user=TelegramUser(
                    id=501,
                    is_bot=False,
                    first_name="Outsider",
                ),
                user_chat_id=501,
                date=datetime.now(UTC),
                invite_link=ChatInviteLink(
                    invite_link="https://t.me/+primary",
                    creator=TelegramUser(
                        id=999,
                        is_bot=True,
                        first_name="PropGate",
                    ),
                    creates_join_request=True,
                    is_primary=False,
                    is_revoked=False,
                ),
            ),
        )
        await dispatcher.feed_update(bot, outsider_update)
    finally:
        await bot.session.close()

    assert approve.await_count == 2
    approve.assert_any_await(chat_id=-1001, user_id=500)
    approve.assert_any_await(chat_id=-1002, user_id=500)
    decline.assert_awaited_once_with(chat_id=-1001, user_id=501)
