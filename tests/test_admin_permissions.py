from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.admin.service import AdminPermissionService
from app.bot.handlers.admin import _channels_text, _payments_text
from app.db.models import Channel, Payment, Plan, User


@pytest.mark.asyncio
async def test_non_admin_denied_and_db_admin_allowed(session, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.admin.service.get_settings",
        lambda: SimpleNamespace(admin_ids=[100]),
    )
    service = AdminPermissionService(session)

    assert not await service.is_admin(999)
    assert service.is_superadmin(100)

    admin = await service.add_admin(200)
    await session.commit()
    assert admin.is_active
    assert await service.is_admin(200)
    assert await service.can_manage(200)


@pytest.mark.asyncio
async def test_restricted_admin_can_manage_only_granted_channel(session, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.admin.service.get_settings",
        lambda: SimpleNamespace(admin_ids=[]),
    )
    channel_a = Channel(telegram_chat_id=-1001, title="A")
    channel_b = Channel(telegram_chat_id=-1002, title="B")
    session.add_all([channel_a, channel_b])
    await session.flush()

    service = AdminPermissionService(session)
    await service.add_admin(200)
    await service.set_all_channels(200, False)
    await service.grant_channel(200, channel_a.id)
    await session.commit()

    assert await service.can_manage_channel(200, channel_a.id)
    assert not await service.can_manage_channel(200, channel_b.id)
    assert await service.channel_scope(200) == [channel_a.id]

    await service.remove_admin(200)
    await session.commit()
    assert await service.channel_scope(200) == []


@pytest.mark.asyncio
async def test_support_role_is_read_only(session, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.admin.service.get_settings",
        lambda: SimpleNamespace(admin_ids=[]),
    )
    service = AdminPermissionService(session)
    await service.add_admin(300, role="support")
    await session.commit()

    assert await service.is_admin(300)
    assert not await service.can_manage(300)
    assert await service.channel_scope(300, require_manage=True) == []


@pytest.mark.asyncio
async def test_restricted_admin_lists_only_scoped_channel_data(session, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.admin.service.get_settings",
        lambda: SimpleNamespace(admin_ids=[]),
    )
    channel_a = Channel(telegram_chat_id=-4001, title="Visible")
    channel_b = Channel(telegram_chat_id=-4002, title="Hidden")
    user = User(telegram_id=401)
    session.add_all([channel_a, channel_b, user])
    await session.flush()
    plans = [
        Plan(
            channel_id=channel.id,
            title=channel.title,
            duration_days=30,
            price=Decimal(100),
            currency="RUB",
        )
        for channel in (channel_a, channel_b)
    ]
    session.add_all(plans)
    await session.flush()
    payments = [
        Payment(
            user_id=user.id,
            plan_id=plan.id,
            provider="dev",
            provider_payment_id=f"provider-{index}",
            amount=Decimal(100),
            currency="RUB",
            idempotency_key=f"payment-{index}",
        )
        for index, plan in enumerate(plans, start=1)
    ]
    session.add_all(payments)
    service = AdminPermissionService(session)
    await service.add_admin(400)
    await service.set_all_channels(400, False)
    await service.grant_channel(400, channel_a.id)
    await session.commit()

    channels_text = await _channels_text(session, 400)
    payments_text = await _payments_text(session, 20, 400)

    assert "Visible" in channels_text
    assert "Hidden" not in channels_text
    assert f"#{payments[0].id}" in payments_text
    assert f"#{payments[1].id}" not in payments_text
