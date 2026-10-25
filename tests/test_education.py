from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.db.models.education import EducationPreference, EducationSignal
from app.education.content import LESSONS, MEDIA, ROOT, TITLES
from app.education.handlers import RiskForm, accept, finish, get_signal
from app.education.keyboards import menu, navigation
from app.education.risk import D, Trade, calculate, number
from app.education.signals import signal_keyboard, usable, validate_signal
from app.education.store import save_preferences


def trade(side="LONG"):
    return Trade(
        "BTC",
        side,
        D(50000),
        D(49000 if side == "LONG" else 51000),
        D(52000 if side == "LONG" else 48000),
    )


def estimate(**kwargs):
    values = {"account_type": "Basic", "nominal": D(10000), "base": D(10000), "leverage": D(5)}
    values.update(kwargs)
    return calculate(trade(), **values)


@pytest.mark.parametrize("size", [5000, 10000, 25000, 50000, 100000])
@pytest.mark.parametrize("risk", ["0.5", "1", "2"])
@pytest.mark.parametrize("leverage", [1, 2, 5])
@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_examples(size, risk, leverage, side):
    result = calculate(
        trade(side),
        account_type="Basic",
        nominal=D(size),
        base=D(size),
        leverage=D(leverage),
        risk_percent=D(risk),
    )
    assert result.loss <= D(size) * D(risk) / 100
    assert D(size) * D(risk) / 100 - result.loss < D(".001")
    assert result.notional == result.margin * leverage
    assert result.profit == result.movement_loss * 2


def test_costs_reduce_size_and_round_down():
    result = estimate(fee_percent=D(".08"), execution_percent=D(".1"), funding_usd=D(3))
    assert result.margin < 1000
    assert result.loss <= 100
    assert result.loss > result.movement_loss
    assert estimate().margin == D(1000)
    assert estimate(risk_percent=D(".5")).margin == D(500)


def test_reverse_and_changed_base():
    assert estimate(margin=D(1000), leverage=D(2)).risk_percent == D(".4")
    assert estimate(margin=D(2500), leverage=D(2)).loss == D(100)
    assert estimate(base=D(8000)).loss == 80


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1", "0", "1e999999", "1e-999999", "abc"])
def test_invalid_numbers(bad):
    with pytest.raises(ValueError):
        number(bad)


def test_decimal_comma():
    assert number("0,5") == D(".5")


@pytest.mark.parametrize("leverage", ["0.5", "3", "5"])
def test_turbo_cap(leverage):
    with pytest.raises(ValueError):
        estimate(account_type="Turbo", leverage=D(leverage))


@pytest.mark.parametrize(
    "side,stop,take",
    [("LONG", 51000, 52000), ("SHORT", 49000, 48000), ("LONG", 50000, 52000), ("X", 49000, 52000)],
)
def test_bad_levels(side, stop, take):
    with pytest.raises(ValueError):
        calculate(
            Trade("BTC", side, D(50000), D(stop), D(take)),
            account_type="Basic",
            nominal=D(10000),
            base=D(10000),
            leverage=D(5),
        )


def test_headroom_and_balance():
    result = estimate(
        free=D(900),
        daily_remaining=D(120),
        total_remaining=D(1000),
        other_risk=D(20),
        equity=D(10000),
    )
    assert any("Недостаточно" in text for text in result.warnings)
    assert any("дневной" in text and "достигает" in text for text in result.warnings)
    assert not any("общей" in text and "достигает" in text for text in result.warnings)
    assert any("неизвестен" in text for text in estimate().warnings)


def test_no_size_when_funding_exhausts_budget():
    with pytest.raises(ValueError):
        estimate(funding_usd=D(100))


def signal():
    return {
        "id": "104",
        "coin": "BTC",
        "side": "LONG",
        "entry": "50000",
        "stop": "49000",
        "take": "52000",
        "status": "active",
        "order_type": "LIMIT",
        "revision": 1,
        "source": "Учебный пример",
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }


def test_signal_validation_status_and_expiry():
    payload = validate_signal({**signal(), "demo": True})
    assert usable(payload)
    for status in ("closed", "cancelled"):
        assert not usable({**payload, "status": status})
    assert not usable({**payload, "expires_at": "2020-01-01T00:00:00+00:00"})
    with pytest.raises(ValueError):
        validate_signal({"id": "104"})
    with pytest.raises(ValueError):
        validate_signal({**signal(), "stop": "51000"})
    with pytest.raises(ValueError):
        validate_signal(signal())  # Real signals must be scoped to a subscription channel.


def test_every_lesson_independently_reachable_and_context_preserved():
    assert set(TITLES) == set(LESSONS)
    destinations = [b.callback_data for row in menu("104").inline_keyboard for b in row]
    for key, pages in LESSONS.items():
        assert f"edu:page:{key}:0:104" in destinations
        for index, page in enumerate(pages):
            assert len(page) < 2500  # Leaves room for signal context within Telegram's limit.
            buttons = [b for row in navigation(key, index, "104").inline_keyboard for b in row]
            assert any(b.callback_data == "edu:menu:104" for b in buttons)
            for item in buttons:
                assert not item.callback_data or len(item.callback_data.encode()) <= 64
                if item.callback_data and item.callback_data.startswith("edu:page:"):
                    _, _, target, number_text, context = item.callback_data.split(":")
                    assert 0 <= int(number_text) < len(LESSONS[target])
                    assert context == "104"
    assert all(
        b.callback_data.endswith(":104")
        for row in signal_keyboard("104").inline_keyboard
        for b in row
    )


async def test_preferences_are_persistent_and_never_store_balances(session):
    values = {
        "account_type": "Turbo",
        "nominal": "10000",
        "risk_percent": "0.5",
        "leverage": "2",
        "equity": "8765",
        "free": "1234",
        "base": "8765",
    }
    await save_preferences(session, 123, values)
    await session.commit()
    record = await session.get(EducationPreference, 123)
    assert record.values == {
        k: values[k] for k in ("account_type", "nominal", "risk_percent", "leverage")
    }
    await save_preferences(session, 123, {**values, "nominal": "5000"})
    await session.commit()
    await session.refresh(record)
    assert record.values["nominal"] == "5000"


async def test_wizard_rejects_turbo_x5_and_reprompts():
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=2, user_id=2))
    await state.set_state(RiskForm.input)
    await state.update_data(field="leverage", values={"account_type": "Turbo"})
    message = AsyncMock()
    await accept(message, state, "5")
    assert "leverage" not in (await state.get_data())["values"]
    assert "недоступно" in message.answer.call_args.args[0]


async def test_signal_changed_mid_calculation_is_rejected():
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=2, user_id=2))
    message = AsyncMock()
    with patch(
        "app.education.handlers.get_signal", new=AsyncMock(return_value={**signal(), "revision": 2})
    ):
        await finish(
            message, state, {"values": {}, "signal": signal(), "signal_id": "104", "user_id": 2}
        )
    assert "изменился" in message.answer.call_args.args[0]


def test_all_media_are_packaged_real_files():
    for names in MEDIA.values():
        for name in names:
            image = ROOT / "assets" / f"{name}.png"
            assert image.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
            assert (ROOT / "originals" / f"{name}.png").is_file()
            assert (ROOT / "assets" / f"{name}.svg").is_file()


@pytest.mark.parametrize("reverse", [False, True])
async def test_complete_wizard_with_manual_inputs_and_persistence(session, reverse):
    @asynccontextmanager
    async def factory():
        yield session

    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=2, user_id=2))
    await state.set_state(RiskForm.input)
    await state.update_data(
        values={}, field="account_type", signal=None, signal_id="-", saved={}, user_id=2
    )
    message = AsyncMock()
    inputs = [
        "Basic",
        "reverse" if reverse else "forward",
        "10000",
        "10000",
        "1000" if reverse else "1",
        "5",
        "BTC",
        "LONG",
        "50000",
        "49000",
        "52000",
        "0",
        "0",
        "0",
        "-",
        "-",
        "-",
        "-",
        "0",
    ]
    with patch("app.education.handlers.async_session_factory", factory):
        for value in inputs:
            await accept(message, state, value)
    assert await state.get_state() is None
    result = message.answer.call_args.args[0]
    assert "Залог: 1000.00" in result
    assert "100.00 USD" in result
    record = await session.get(EducationPreference, 2)
    assert record.values["nominal"] == "10000"
    assert "equity" not in record.values


async def test_private_signal_context_is_not_public(session):
    @asynccontextmanager
    async def factory():
        yield session

    session.add(EducationSignal(id="104", payload={**signal(), "channel_id": 999}))
    await session.commit()
    with (
        patch("app.education.handlers.async_session_factory", factory),
        patch(
            "app.education.handlers.AdminPermissionService.can_manage_channel",
            new=AsyncMock(return_value=False),
        ),
    ):
        assert await get_signal("104", 1234) is None
        record = await session.get(EducationSignal, "104")
        record.payload = {**signal(), "demo": True}
        await session.commit()
        assert (await get_signal("104", 1234))["coin"] == "BTC"
