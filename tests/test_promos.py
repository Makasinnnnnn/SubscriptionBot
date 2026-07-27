from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import func, select

from app.db.models import (
    Channel,
    PromoCode,
    PromoRedemption,
    Subscription,
)
from app.services.promo_service import PromoRedemptionError, PromoService


class PromoBot:
    def __init__(self, fail_chat: int | None = None) -> None:
        self.fail_chat = fail_chat
        self.invites: list[int] = []
        self.revoked: list[tuple[int, str]] = []

    async def get_me(self):
        return SimpleNamespace(id=999)

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(
            status=ChatMemberStatus.ADMINISTRATOR,
            can_invite_users=True,
            can_restrict_members=True,
        )

    async def create_chat_invite_link(
        self,
        chat_id,
        name,
        expire_date,
        creates_join_request,
    ):
        if chat_id == self.fail_chat:
            raise TelegramBadRequest(method=None, message="failed")
        self.invites.append(chat_id)
        return SimpleNamespace(invite_link=f"https://t.me/+{name}")

    async def revoke_chat_invite_link(self, chat_id, invite_link):
        self.revoked.append((chat_id, invite_link))


async def _promo_fixture(session, *, valid_from=None, valid_until=None, limit=2):
    now = datetime.now(UTC).replace(tzinfo=None)
    channel = Channel(telegram_chat_id=-1001, title="Deals")
    session.add(channel)
    await session.flush()
    from app.db.models import Plan

    plan = Plan(
        channel_id=channel.id,
        title="Month",
        duration_days=30,
        price=Decimal("4990"),
        currency="RUB",
    )
    session.add(plan)
    await session.flush()
    promo = PromoCode(
        code="FLY2026",
        plan_id=plan.id,
        valid_from=valid_from or now - timedelta(hours=1),
        valid_until=valid_until or now + timedelta(days=1),
        max_redemptions=limit,
        per_user_limit=1,
    )
    session.add(promo)
    await session.commit()
    return promo, plan


def _patch_promo_settings(monkeypatch, materials_channel_id=None) -> None:
    settings = SimpleNamespace(materials_channel_id=materials_channel_id)
    monkeypatch.setattr("app.services.promo_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.subscriptions.service.get_settings", lambda: settings)

    async def no_notify(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "app.subscriptions.service.SubscriptionService._notify_admins",
        no_notify,
    )


@pytest.mark.asyncio
async def test_promo_redeems_once_and_extends_existing_subscription(
    session,
    monkeypatch,
) -> None:
    _patch_promo_settings(monkeypatch)
    promo, plan = await _promo_fixture(session)

    result = await PromoService(session).redeem(500, "  fly2026 ", PromoBot())
    await session.commit()

    assert result.subscription.source == "promo"
    assert result.subscription.channel_id == plan.channel_id
    assert result.primary_link.startswith("https://t.me/+")
    assert promo.redemptions_count == 1
    with pytest.raises(PromoRedemptionError, match="уже использовали"):
        await PromoService(session).redeem(500, "FLY2026", PromoBot())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("from_delta", "until_delta", "message"),
    [
        (timedelta(hours=1), timedelta(days=1), "ещё не начал"),
        (-timedelta(days=2), -timedelta(hours=1), "закончился"),
    ],
)
async def test_promo_validity_window(
    session,
    monkeypatch,
    from_delta,
    until_delta,
    message,
) -> None:
    _patch_promo_settings(monkeypatch)
    now = datetime.now(UTC).replace(tzinfo=None)
    await _promo_fixture(
        session,
        valid_from=now + from_delta,
        valid_until=now + until_delta,
    )

    with pytest.raises(PromoRedemptionError, match=message):
        await PromoService(session).redeem(500, "FLY2026", PromoBot(), now=now)


@pytest.mark.asyncio
async def test_promo_limit_and_archived_plan_are_rejected(
    session,
    monkeypatch,
) -> None:
    _patch_promo_settings(monkeypatch)
    promo, plan = await _promo_fixture(session, limit=1)
    promo.redemptions_count = 1
    await session.commit()
    with pytest.raises(PromoRedemptionError, match="Лимит"):
        await PromoService(session).redeem(500, "FLY2026", PromoBot())

    promo.redemptions_count = 0
    plan.is_active = False
    await session.commit()
    with pytest.raises(PromoRedemptionError, match="недоступен"):
        await PromoService(session).redeem(501, "FLY2026", PromoBot())


@pytest.mark.asyncio
async def test_failed_materials_invite_does_not_consume_promo(
    session,
    monkeypatch,
) -> None:
    materials_id = -1002
    _patch_promo_settings(monkeypatch, materials_id)
    promo, _ = await _promo_fixture(session)

    bot = PromoBot(fail_chat=materials_id)
    with pytest.raises(TelegramBadRequest):
        await PromoService(session).redeem(
            500,
            "FLY2026",
            bot,
        )
    await session.rollback()

    assert (
        await session.scalar(select(func.count(PromoRedemption.id)))
        == 0
    )
    assert await session.scalar(select(func.count(Subscription.id))) == 0
    await session.refresh(promo)
    assert promo.redemptions_count == 0
    assert bot.revoked == [(-1001, "https://t.me/+sub-1")]
