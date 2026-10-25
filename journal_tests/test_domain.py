from decimal import Decimal

import pytest

from app.journal.domain import (
    JournalError,
    date_serial,
    net_pnl,
    number,
    qty_from_risk,
    risk_amount,
    sheet_inputs,
    timestamp,
    validate_trade,
)


def data(**kwargs):
    return {"account": "1", "symbol": "BTCUSDT", "side": "Long", "entry": "100", "stop": "90",
            "exit": "110", "quantity": "10", "leverage": "5", "balance": "200000",
            "costs": "2", "opened": "2026-10-25T00:00:00+00:00",
            "closed": "2026-10-26T00:00:00+00:00", **kwargs}


@pytest.mark.parametrize("side,exit,expected", [("Long", "110", "98"), ("Short", "90", "98"),
                                                 ("Long", "90", "-102")])
def test_profit(side, exit, expected):
    assert net_pnl(data(side=side, exit=exit)) == Decimal(expected)


def test_zero_actual_and_funding():
    assert net_pnl(data(actual="0")) == 0
    assert net_pnl(data(costs="-2")) == 102
    assert risk_amount(data(stop=None)) is None
    assert risk_amount(data(stop="100")) == 0
    assert qty_from_risk(data(), Decimal(1)) == 200


@pytest.mark.parametrize("raw,expected", [("1 234,56", "1234.56"), ("0,001", "0.001"),
                                          ("-0.50", "-0.50"), ("200000", "200000")])
def test_numbers(raw, expected):
    assert number(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["1,234", "12.345", "NaN", "Infinity", "1e5", "1,234.56", "1 2"])
def test_ambiguous_numbers(raw):
    with pytest.raises(JournalError):
        number(raw)


def test_utc7_dates_and_literal_comment():
    value = timestamp("25.10.2026 14:30", "Asia/Tomsk")
    assert value == "2026-10-25T07:30:00+00:00"
    assert date_serial(value, "Asia/Tomsk") % 1 == pytest.approx(14.5 / 24)
    assert sheet_inputs(data(comment="=IMPORTXML(\"private\")"), "Asia/Tomsk")["23"].startswith("=")
    with pytest.raises(JournalError):
        validate_trade(data(closed="2026-10-24T00:00:00+00:00"))
