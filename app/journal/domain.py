import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

TRADE_HEADERS = [
    "ID сделки", "ID счёта", "Инструмент", "Направление", "Открыли — дата и время",
    "Закрыли — дата и время", "Длительность, ч", "Цена входа", "Стоп — цена",
    "Цена выхода", "Плечо, ×", "Количество монет", "Баланс перед входом, $",
    "Издержки, $", "Факт чистый PnL, $", "Убыток до стопа, $", "Риск от баланса, %",
    "Расчёт валовый PnL, $", "Чистый PnL, $", "PnL от баланса, %", "Движение цены, %",
    "PnL от маржи, %", "Статус", "Комментарий", "Скриншотов", "Ссылка на скрин / папку",
]
SHOT_HEADERS = ["ID сделки", "Тип скрина", "Дата и время скрина", "Ссылка на оригинал",
                "Изображение в ячейке", "Комментарий"]
ACCOUNT_HEADERS = ["ID счёта", "Название", "Начальный баланс, $", "Валюта",
                   "Реализованный PnL, $", "Доходность, %", "Начальный + PnL, $",
                   "Закрытых сделок", "Открытых сделок", "Комментарий"]
INPUT_COLUMNS = {1: "account", 2: "symbol", 3: "side", 4: "opened", 5: "closed",
                 7: "entry", 8: "stop", 9: "exit", 10: "leverage", 11: "quantity",
                 12: "balance", 13: "costs", 14: "actual", 23: "comment", 25: "shot_url"}
FORMULA_COLUMNS = (6, 15, 16, 17, 18, 19, 20, 21, 22, 24)
SHOT_TYPES = ["Вход", "Закрытие", "Стоп", "Изменение", "Другое"]


class JournalError(Exception):
    """Expected, safe-to-display validation error."""


class SyncError(JournalError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class ConflictError(SyncError):
    pass


def number(raw: str, positive: bool = False) -> Decimal:
    raw = raw.strip().replace("\u00a0", " ").replace("\u202f", " ")
    if not raw or len(raw) > 80:
        raise JournalError("Введите число, например 1234,56.")
    if " " in raw:
        if not re.fullmatch(r"[+-]?\d{1,3}(?: \d{3})+(?:[.,]\d+)?", raw):
            raise JournalError("Неясные разделители. Пример: 1 234,56 или 1234.56.")
        raw = raw.replace(" ", "")
    # Do not guess whether the sole separator is a grouping or decimal separator.
    if re.fullmatch(r"[+-]?[1-9]\d{0,2}[.,]\d{3}", raw):
        raise JournalError(
            f"Неоднозначное число {raw}: для дробного допишите ноль (например {raw}0), "
            "для целого уберите разделитель."
        )
    if not re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", raw):
        raise JournalError("Введите обычное число без валюты, %, × и экспоненты.")
    try:
        value = Decimal(raw.replace(",", "."))
    except InvalidOperation as exc:
        raise JournalError("Некорректное число.") from exc
    if not value.is_finite() or abs(value) > Decimal("1e15"):
        raise JournalError("Число слишком большое.")
    if positive and value <= 0:
        raise JournalError("Значение должно быть больше нуля.")
    return value


def timestamp(raw: str, timezone: str) -> str:
    try:
        parsed = datetime.strptime(raw.strip(), "%d.%m.%Y %H:%M")
    except ValueError as exc:
        raise JournalError("Формат даты: 25.10.2026 14:30, время Томска.") from exc
    return parsed.replace(tzinfo=ZoneInfo(timezone)).astimezone(UTC).isoformat()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def date_serial(value: str, timezone: str) -> float:
    local = datetime.fromisoformat(value).astimezone(ZoneInfo(timezone)).replace(tzinfo=None)
    return (local - datetime(1899, 12, 30)) / timedelta(days=1)


def show_time(value: str, timezone: str) -> str:
    return datetime.fromisoformat(value).astimezone(ZoneInfo(timezone)).strftime("%d.%m.%Y %H:%M")


def qty_from_risk(data: dict, risk: Decimal) -> Decimal:
    if risk <= 0 or risk > 100:
        raise JournalError("Риск должен быть больше 0 и не больше 100%.")
    distance = abs(Decimal(data["entry"]) - Decimal(data.get("stop") or data["entry"]))
    if not distance:
        raise JournalError("Для расчёта нужен стоп с ненулевым расстоянием от входа.")
    if ((data["side"] == "Long" and Decimal(data["stop"]) > Decimal(data["entry"])) or
            (data["side"] == "Short" and Decimal(data["stop"]) < Decimal(data["entry"]))):
        raise JournalError("Для расчёта количества по риску нужен стоп с убыточной стороны входа.")
    return Decimal(data["balance"]) * risk / 100 / distance


def risk_amount(data: dict) -> Decimal | None:
    if data.get("stop") is None:
        return None
    delta = Decimal(data["entry"]) - Decimal(data["stop"])
    if data["side"] == "Short":
        delta = -delta
    return max(Decimal(0), delta * Decimal(data["quantity"]))


def net_pnl(data: dict) -> Decimal:
    if data.get("actual") is not None:
        return Decimal(data["actual"])
    delta = Decimal(data["exit"]) - Decimal(data["entry"])
    if data["side"] == "Short":
        delta = -delta
    return delta * Decimal(data["quantity"]) - Decimal(data.get("costs") or "0")


def validate_trade(data: dict) -> None:
    for key in ("account", "symbol", "side", "opened", "entry", "leverage", "quantity", "balance"):
        if data.get(key) in (None, ""):
            raise JournalError(f"Не заполнено поле: {key}")
    if data["side"] not in ("Long", "Short"):
        raise JournalError("Направление: Long или Short.")
    for key in ("entry", "leverage", "quantity", "balance"):
        if Decimal(data[key]) <= 0:
            raise JournalError(f"{key}: требуется положительное число.")
    if data.get("stop") is not None and Decimal(data["stop"]) <= 0:
        raise JournalError("Цена стопа должна быть положительной.")
    opened = datetime.fromisoformat(data["opened"])
    if opened.tzinfo is None:
        raise JournalError("Время должно содержать часовой пояс.")
    if data.get("closed"):
        if datetime.fromisoformat(data["closed"]) < opened:
            raise JournalError("Закрытие не может быть раньше открытия.")
        if Decimal(data.get("exit") or "0") <= 0:
            raise JournalError("Укажите положительную цену выхода.")


def sheet_inputs(data: dict, timezone: str) -> dict[str, str | int | float]:
    result: dict[str, str | int | float] = {}
    for col, key in INPUT_COLUMNS.items():
        value = data.get(key)
        if value is None or value == "":
            result[str(col)] = ""
        elif key in ("opened", "closed"):
            result[str(col)] = date_serial(value, timezone)
        elif key in ("account", "symbol", "side", "comment", "shot_url"):
            result[str(col)] = str(value)
        else:
            result[str(col)] = float(Decimal(str(value)))
    return result


def cell_values_equal(left, right) -> bool:
    if left in (None, "") and right in (None, ""):
        return True
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(Decimal(str(left)) - Decimal(str(right))) <= Decimal("0.000000001")
    return str(left) == str(right)
