import re
from datetime import UTC, datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.education.risk import Trade, number

STATUSES = {"active": "актуален", "changed": "изменён", "cancelled": "отменён", "closed": "закрыт"}


def validate_signal(payload: dict) -> dict:
    required = {
        "id",
        "coin",
        "side",
        "entry",
        "stop",
        "take",
        "order_type",
        "status",
        "source",
        "expires_at",
        "revision",
    }
    if not isinstance(payload, dict) or not required <= payload.keys():
        raise ValueError("Не хватает обязательных полей сигнала")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", str(payload["id"])):
        raise ValueError("ID: 1–24 латинских букв, цифр, _ или -")
    if not re.fullmatch(r"[A-Z0-9]{1,16}", str(payload["coin"])):
        raise ValueError("Некорректный тикер")
    if payload["status"] not in STATUSES or payload["order_type"] not in {"MARKET", "LIMIT"}:
        raise ValueError("Неизвестный статус или тип входа")
    if not isinstance(payload["source"], str) or not 1 <= len(payload["source"].strip()) <= 200:
        raise ValueError("Укажите источник сигнала (до 200 символов)")
    if type(payload["revision"]) is not int or not 1 <= payload["revision"] <= 1000000:
        raise ValueError("revision должен быть положительным целым числом")
    try:
        expires = datetime.fromisoformat(payload["expires_at"])
        if expires.tzinfo is None:
            raise ValueError("Укажите часовой пояс срока действия")
    except (TypeError, ValueError) as exc:
        raise ValueError("expires_at: ISO-дата с часовым поясом") from exc
    trade_from_signal(payload).validate()
    result = {key: payload[key] for key in required}
    demo = payload.get("demo", False)
    channel_id = payload.get("channel_id")
    if type(demo) is not bool:
        raise ValueError("demo: true или false")
    if channel_id is not None and (type(channel_id) is not int or channel_id <= 0):
        raise ValueError("channel_id — внутренний положительный ID канала")
    if channel_id is None and not demo:
        raise ValueError(
            "Для закрытого сигнала нужен channel_id; публичный пример требует demo:true"
        )
    result.update(demo=demo, channel_id=channel_id)
    for key in ("entry", "stop", "take"):
        result[key] = str(number(payload[key]))
    return result


def trade_from_signal(payload: dict) -> Trade:
    return Trade(
        payload["coin"],
        payload["side"],
        number(payload["entry"]),
        number(payload["stop"]),
        number(payload["take"]),
    )


def usable(payload: dict) -> bool:
    return payload["status"] in {"active", "changed"} and datetime.fromisoformat(
        payload["expires_at"]
    ) > datetime.now(UTC)


def signal_text(payload: dict) -> str:
    return (
        f"{payload['coin']} {payload['side']} · {STATUSES[payload['status']]}\n"
        f"Сигнал {payload['id']} · версия {payload['revision']}\n"
        f"Вход {payload['entry']} USD · {payload['order_type']}\n"
        f"SL {payload['stop']} · TP {payload['take']}\n"
        f"Срок: {payload['expires_at']}\nИсточник: {payload['source']}\n"
        + (
            ""
            if usable(payload)
            else "Вход по этому сигналу недоступен: статус или срок изменён.\n"
        )
        + "Состояние вашей позиции проверяйте в Upscale. Сначала уточните допустимость "
        "использования сигналов; обучение — на демо-счёте."
    )


def signal_keyboard(signal_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=title, callback_data=f"edu:page:{key}:0:{signal_id}")]
            for title, key in (
                ("Как открыть эту сделку", "open"),
                ("Как выставить SL/TP", "protect"),
                ("Как закрыть позицию", "close"),
            )
        ]
        + [
            [
                InlineKeyboardButton(
                    text="Рассчитать мой риск", callback_data=f"edu:calc:{signal_id}"
                )
            ]
        ]
    )
