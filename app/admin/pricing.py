from decimal import Decimal, InvalidOperation

from app.payments.money import normalized_amount, quantum


def parse_plan_price(raw: str | None) -> Decimal:
    try:
        value = Decimal((raw or "").strip().replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("Введите положительную цену не выше 9999999999.99.") from exc
    if (
        not value.is_finite()
        or not Decimal("0.000000000000000001") <= value <= Decimal("9999999999.99")
        or value.normalize().as_tuple().exponent < -18
    ):
        raise ValueError("Введите положительную цену не выше 9999999999.99.")
    return value


def validate_plan_price(value: Decimal, currency: str) -> Decimal:
    value = parse_plan_price(str(value))
    try:
        step = quantum(currency)
    except ValueError as exc:
        raise ValueError("Валюта не поддерживается платёжными системами.") from exc
    try:
        return normalized_amount(value, currency)
    except (ValueError, InvalidOperation) as exc:
        if currency.upper() == "XTR":
            raise ValueError("Для Stars цена должна быть целым числом.") from exc
        digits = -step.as_tuple().exponent
        raise ValueError(
            f"Для {currency.upper()} укажите не более {digits} знаков после запятой."
        ) from exc
