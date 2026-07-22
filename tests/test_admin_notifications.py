from types import SimpleNamespace

import pytest

from app.admin.service import AdminPermissionService
from app.db.models import AdminNotificationDelivery, Channel
from app.services.admin_notification_service import AdminNotificationService


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, telegram_id: int, text: str) -> None:
        self.messages.append((telegram_id, text))


@pytest.mark.asyncio
async def test_notification_scope_preferences_and_deduplication(session, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.admin.service.get_settings",
        lambda: SimpleNamespace(admin_ids=[]),
    )
    monkeypatch.setattr(
        "app.services.admin_notification_service.get_settings",
        lambda: SimpleNamespace(admin_ids=[]),
    )
    channel_a = Channel(telegram_chat_id=-1001, title="A")
    channel_b = Channel(telegram_chat_id=-1002, title="B")
    session.add_all([channel_a, channel_b])
    await session.flush()
    permissions = AdminPermissionService(session)
    await permissions.add_admin(100)
    await permissions.set_all_channels(100, False)
    await permissions.grant_channel(100, channel_a.id)
    await permissions.add_admin(200)
    await session.commit()

    bot = FakeBot()
    service = AdminNotificationService(session, bot)
    await service.set_preference(200, "payments", False)
    sent = await service.notify("payments", "paid", "Payment", "payment-1", channel_a.id)
    repeated = await service.notify("payments", "paid", "Payment", "payment-1", channel_a.id)
    hidden = await service.notify("payments", "paid", "Other", "payment-2", channel_b.id)
    await session.commit()

    assert sent == 1
    assert repeated == 0
    assert hidden == 0
    assert bot.messages == [(100, "Payment")]
    deliveries = list(await session.scalars(AdminNotificationDelivery.__table__.select()))
    assert len(deliveries) == 1
