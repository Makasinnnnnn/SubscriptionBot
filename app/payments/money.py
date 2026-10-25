from decimal import ROUND_HALF_UP, Decimal

ASSET_SCALES = {
    "BTC": 8,
    "ETH": 18,
    "TON": 9,
    "LTC": 8,
    "BNB": 8,
    "TRX": 6,
    "USDT": 6,
    "USDC": 6,
    "XTR": 0,
}
FIAT_CURRENCIES = {
    "USD",
    "EUR",
    "RUB",
    "BYN",
    "UAH",
    "GBP",
    "CNY",
    "KZT",
    "UZS",
    "GEL",
    "TRY",
    "AMD",
    "THB",
    "INR",
    "BRL",
    "IDR",
    "AZN",
    "AED",
    "PLN",
    "ILS",
}


def quantum(currency: str) -> Decimal:
    currency = currency.upper()
    if currency not in ASSET_SCALES and currency not in FIAT_CURRENCIES:
        raise ValueError("Unsupported payment currency")
    return Decimal(1).scaleb(-ASSET_SCALES.get(currency, 2))


def normalized_amount(amount: Decimal, currency: str) -> Decimal:
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Payment amount must be positive and finite")
    step = quantum(currency)
    result = amount.quantize(step, rounding=ROUND_HALF_UP)
    if result <= 0 or result != amount:
        raise ValueError("Payment amount has unsupported currency precision")
    return result


def format_amount(amount: Decimal, currency: str) -> str:
    return format(normalized_amount(amount, currency), "f")
