from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.bot.handlers import admin_ui
from app.bot.handlers.channels import _part_int
from app.bot.handlers.admin_ui import (
    _plan_field_value,
    _positive_int,
    _price,
    _resolve_user_reference,
    _show_grant_channels,
    _star_amount,
)
from app.db.models import User


@pytest.mark.parametrize(
    ("value", "expected"),
    [("chan:view:12", 12), ("chan:view:x", None), (None, None)],
)
def test_channel_callback_ids_are_strictly_parsed(
    value: str | None, expected: int | None
) -> None:
    assert _part_int(value, 2) == expected


def test_price_accepts_comma_and_rounds_to_cents() -> None:
    assert _price("12,5") == Decimal("12.50")


@pytest.mark.parametrize("value", ["0", "-1", "not-a-price", "10000000000"])
def test_price_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        _price(value)


def test_plan_field_validation() -> None:
    assert _plan_field_value("currency", "usdt") == "USDT"
    assert _plan_field_value("duration", "30") == 30
    with pytest.raises(ValueError):
        _plan_field_value("currency", "USD")


def test_positive_int_range() -> None:
    assert _positive_int("3650", 1, 3650) == 3650
    with pytest.raises(ValueError):
        _positive_int("3651", 1, 3650)


def test_star_amount_formats_nanostars() -> None:
    assert _star_amount(12, None) == "12"
    assert _star_amount(12, 500_000_000) == "12.5"


@pytest.mark.asyncio
async def test_resolve_user_by_id_or_username(session) -> None:
    user = User(telegram_id=987654321, username="Known_User")
    session.add(user)
    await session.commit()

    assert await _resolve_user_reference(session, "987654321") == 987654321
    assert await _resolve_user_reference(session, "@known_user") == 987654321
    assert await _resolve_user_reference(session, "KNOWN_USER") == 987654321
    assert await _resolve_user_reference(session, "@missing") is None


@pytest.mark.asyncio
async def test_grant_menu_uses_actor_id_for_channel_scope(monkeypatch) -> None:
    actor_ids: list[int] = []

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    class FakeMessage:
        def __init__(self) -> None:
            self.answers: list[tuple[str, object]] = []

        async def answer(self, text: str, reply_markup=None) -> None:
            self.answers.append((text, reply_markup))

    async def managed_channels(_session, actor_telegram_id: int):
        actor_ids.append(actor_telegram_id)
        return [SimpleNamespace(id=7, title="Deals")]

    monkeypatch.setattr(admin_ui, "async_session_factory", SessionContext)
    monkeypatch.setattr(admin_ui, "_managed_channels", managed_channels)
    message = FakeMessage()

    await _show_grant_channels(message, telegram_id=123, actor_telegram_id=456)

    assert actor_ids == [456]
    assert len(message.answers) == 1
    keyboard = message.answers[0][1]
    assert keyboard.inline_keyboard[0][0].callback_data == "adm:grantch:123:7"
