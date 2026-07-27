from decimal import Decimal

import pytest

from app.admin.settings_service import RuntimeSettingsService
from app.bot.handlers.subscriptions import _available_plans, _plan_is_available
from app.db.models import Channel, Plan, User
from app.services.payment_service import PaymentService


@pytest.mark.asyncio
async def test_materials_channel_is_not_sold_as_a_separate_plan(
    session,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.bot.handlers.subscriptions.get_settings",
        lambda: type("Settings", (), {"materials_channel_id": None})(),
    )
    primary = Channel(telegram_chat_id=-1001, title="PropDeals")
    materials = Channel(telegram_chat_id=-1002, title="PropMethods")
    session.add_all([primary, materials])
    await session.flush()
    primary_plan = Plan(
        channel_id=primary.id,
        title="Основной тариф",
        duration_days=30,
        price=Decimal("4990"),
        currency="RUB",
    )
    materials_plan = Plan(
        channel_id=materials.id,
        title="Старый тариф методичек",
        duration_days=30,
        price=Decimal("4990"),
        currency="RUB",
    )
    session.add_all([primary_plan, materials_plan])
    await RuntimeSettingsService(session).set(
        RuntimeSettingsService.MATERIALS_CHANNEL_ID,
        materials.telegram_chat_id,
        actor_telegram_id=1,
    )

    visible = await _available_plans(session)

    assert [plan.id for plan in visible] == [primary_plan.id]
    assert await _plan_is_available(session, primary_plan)
    assert not await _plan_is_available(session, materials_plan)


@pytest.mark.asyncio
async def test_payment_service_rejects_materials_plan_even_without_handler(
    session,
    monkeypatch,
) -> None:
    primary = Channel(telegram_chat_id=-1001, title="PropDeals")
    materials = Channel(telegram_chat_id=-1002, title="PropMethods")
    user = User(telegram_id=500)
    session.add_all([primary, materials, user])
    await session.flush()
    materials_plan = Plan(
        channel_id=materials.id,
        title="Служебный тариф",
        duration_days=30,
        price=Decimal("4990"),
        currency="RUB",
    )
    session.add(materials_plan)
    await RuntimeSettingsService(session).set(
        RuntimeSettingsService.MATERIALS_CHANNEL_ID,
        materials.telegram_chat_id,
        actor_telegram_id=1,
    )

    async def accepted(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(
        "app.services.payment_service.LegalService.require",
        accepted,
    )
    monkeypatch.setattr(
        "app.services.payment_service.get_payment_provider",
        lambda _name: object(),
    )

    with pytest.raises(ValueError, match="Service plan"):
        await PaymentService(session).create_payment(user, materials_plan, "dev")
