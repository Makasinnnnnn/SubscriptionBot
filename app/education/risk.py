"""Decimal-only estimates for linear USD crypto positions."""

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation, localcontext

D = Decimal
ZERO = D(0)
ONE = D(1)
CAPS = {"Basic": D(5), "Accelerated": D(5), "Turbo": D(2)}


def number(value: str | Decimal, *, zero: bool = False) -> Decimal:
    try:
        text = str(value).strip().replace(",", ".")
        if len(text) > 32:
            raise ValueError("Слишком длинное число")
        result = D(text)
        if not result.is_finite() or result < 0 or (result == 0 and not zero):
            raise ValueError("Введите положительное конечное число")
        if result > D("1e12") or (result != 0 and result < D("1e-12")):
            raise ValueError("Число вне поддерживаемого диапазона")
        return result
    except InvalidOperation as exc:
        raise ValueError("Введите число, например 0,5") from exc


@dataclass(frozen=True)
class Trade:
    coin: str
    side: str
    entry: Decimal
    stop: Decimal
    take: Decimal

    def validate(self) -> None:
        for value in (self.entry, self.stop, self.take):
            number(value)
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("Направление: LONG или SHORT")
        if self.side == "LONG" and not self.stop < self.entry < self.take:
            raise ValueError("Для LONG требуется SL < вход < TP")
        if self.side == "SHORT" and not self.take < self.entry < self.stop:
            raise ValueError("Для SHORT требуется TP < вход < SL")


@dataclass(frozen=True)
class Estimate:
    margin: Decimal
    notional: Decimal
    quantity: Decimal
    movement_loss: Decimal
    estimated_costs: Decimal
    loss: Decimal
    profit: Decimal
    risk_percent: Decimal
    margin_percent: Decimal
    warnings: tuple[str, ...]


def calculate(
    trade: Trade,
    *,
    account_type: str,
    nominal: Decimal,
    base: Decimal,
    leverage: Decimal,
    risk_percent: Decimal = ONE,
    margin: Decimal | None = None,
    equity: Decimal | None = None,
    free: Decimal | None = None,
    daily_remaining: Decimal | None = None,
    total_remaining: Decimal | None = None,
    other_risk: Decimal = ZERO,
    fee_percent: Decimal = ZERO,
    execution_percent: Decimal = ZERO,
    funding_usd: Decimal = ZERO,
) -> Estimate:
    """Fee is per side; execution reserve is total % of entry notional.

    Remaining drawdowns are current USD headroom read by the user from Upscale.
    They are not inferred from the nominal or persisted as live account data.
    """
    trade.validate()
    if account_type not in CAPS:
        raise ValueError("Выберите Basic, Accelerated или Turbo")
    for value in (nominal, base, leverage, risk_percent):
        number(value)
    if leverage < 1 or leverage > CAPS[account_type]:
        raise ValueError(f"Криптовалюты {account_type}: плечо от x1 до x{CAPS[account_type]}")
    if risk_percent > 100:
        raise ValueError("Риск не может превышать 100% базы")
    for value in (other_risk, fee_percent, execution_percent, funding_usd):
        number(value, zero=True)
    for value in (equity, free, daily_remaining, total_remaining):
        if value is not None:
            number(value, zero=True)
    if fee_percent > 100 or execution_percent > 100:
        raise ValueError("Проверьте процент издержек")
    with localcontext() as ctx:
        ctx.prec = 50
        distance = abs(trade.entry - trade.stop) / trade.entry
        upside = abs(trade.take - trade.entry) / trade.entry
        fee = fee_percent / 100
        reserve = execution_percent / 100
        # Conservative close commission for LONG and SHORT alike.
        stop_cost = fee * (1 + trade.stop / trade.entry) + reserve
        take_cost = fee * (1 + trade.take / trade.entry) + reserve
        if margin is None:
            budget = base * risk_percent / 100 - funding_usd
            if budget <= 0:
                raise ValueError("Резерв фандинга исчерпывает бюджет риска")
            margin = (budget / (leverage * (distance + stop_cost))).quantize(
                D(".01"), rounding=ROUND_DOWN
            )
        else:
            number(margin)
            if margin != margin.quantize(D(".01"), rounding=ROUND_DOWN):
                raise ValueError("Залог задаётся с точностью до цента")
        notional = margin * leverage
        if notional < 10:
            raise ValueError("Расчётный объём меньше $10; не увеличивайте риск автоматически")
        movement = notional * distance
        costs = notional * stop_cost + funding_usd
        loss = movement + costs
        warnings = [
            "Оценка, не гарантированный максимальный убыток. Издержки и исполнение могут отличаться.",
            "Шаг объёма, ликвидация и текущие ограничения инструмента проверяются в терминале.",
        ]
        if fee_percent == 0 or execution_percent == 0:
            warnings.append("Нулевые издержки — допущение; комиссии/спред не подтверждены.")
        if equity is None:
            warnings.append("Текущая equity неизвестна боту.")
        elif equity <= 0 or margin > equity:
            warnings.append("Залог превышает указанную equity или equity равна нулю.")
        required = margin + notional * (fee + reserve)
        if free is None:
            warnings.append("Свободный баланс неизвестен; достаточность средств не проверена.")
        elif required > free:
            warnings.append("Недостаточно указанного свободного баланса с резервом на вход.")
        for label, remaining in (("дневной", daily_remaining), ("общей", total_remaining)):
            if remaining is None:
                if label != "дневной" or account_type != "Turbo":
                    warnings.append(f"Остаток {label} просадки неизвестен; лимит не проверен.")
            elif loss + other_risk >= remaining:
                warnings.append(f"Риск сделки и других позиций достигает лимита {label} просадки.")
        if loss > margin:
            warnings.append(
                "Убыток до SL превышает залог: ликвидация может наступить раньше стопа."
            )
        return Estimate(
            margin=margin,
            notional=notional,
            quantity=(notional / trade.entry).quantize(D(".00000001"), rounding=ROUND_DOWN),
            movement_loss=movement,
            estimated_costs=costs,
            loss=loss,
            profit=notional * (upside - take_cost) - funding_usd,
            risk_percent=loss / base * 100,
            margin_percent=margin / nominal * 100,
            warnings=tuple(warnings),
        )


def money(value: Decimal) -> str:
    with localcontext() as ctx:
        ctx.prec = 80
        return str(value.quantize(D(".01"), rounding=ROUND_UP))
