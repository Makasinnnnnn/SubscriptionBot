from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db.enums import SubscriptionStatus
from app.db.models import Channel, NotificationLog, Subscription, User
from app.subscriptions.service import SubscriptionService


class ReminderBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


async def _subscription(
    session,
    *,
    duration: timedelta,
    remaining: timedelta,
) -> Subscription:
    now = datetime.now(UTC).replace(tzinfo=None)
    channel = Channel(telegram_chat_id=-1001, title="Deals")
    user = User(telegram_id=500)
    session.add_all([channel, user])
    await session.flush()
    item = Subscription(
        user_id=user.id,
        channel_id=channel.id,
        status=SubscriptionStatus.ACTIVE,
        starts_at=now - (duration - remaining),
        period_started_at=now - (duration - remaining),
        expires_at=now + remaining,
        source="manual",
    )
    session.add(item)
    await session.commit()
    return item


@pytest.mark.asyncio
async def test_one_day_subscription_skips_long_thresholds_and_sends_12h(
    session,
) -> None:
    await _subscription(
        session,
        duration=timedelta(days=1),
        remaining=timedelta(hours=11, minutes=50),
    )
    bot = ReminderBot()

    sent = await SubscriptionService(session).send_expiration_reminders(
        bot,
        [10080, 4320, 1440, 720],
    )

    assert sent == 1
    assert len(bot.messages) == 1
    assert "UTC" in bot.messages[0][1]


@pytest.mark.asyncio
async def test_scheduler_sends_only_closest_crossed_threshold_and_deduplicates(
    session,
) -> None:
    item = await _subscription(
        session,
        duration=timedelta(days=30),
        remaining=timedelta(hours=10),
    )
    bot = ReminderBot()
    service = SubscriptionService(session)

    first = await service.send_expiration_reminders(
        bot,
        [10080, 4320, 1440, 720],
    )
    second = await service.send_expiration_reminders(
        bot,
        [10080, 4320, 1440, 720],
    )

    assert (first, second) == (1, 0)
    assert len(bot.messages) == 1
    logs = await session.scalar(
        select(func.count(NotificationLog.id)).where(NotificationLog.subscription_id == item.id)
    )
    assert logs == 1


@pytest.mark.asyncio
async def test_extension_allows_new_period_reminder(session) -> None:
    item = await _subscription(
        session,
        duration=timedelta(days=7),
        remaining=timedelta(hours=11),
    )
    bot = ReminderBot()
    service = SubscriptionService(session)
    assert await service.send_expiration_reminders(bot, [720]) == 1

    now = datetime.now(UTC).replace(tzinfo=None)
    item.period_started_at = now - timedelta(hours=13)
    item.expires_at = now + timedelta(hours=11)
    await session.commit()

    assert await service.send_expiration_reminders(bot, [720]) == 1
    assert len(bot.messages) == 2
