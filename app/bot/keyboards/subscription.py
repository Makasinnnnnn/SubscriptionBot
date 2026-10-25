from decimal import Decimal

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.config import get_settings
from app.db.models import Plan

PAYMENT_PROVIDERS = {
    "dev": "Тестовая оплата",
    "yookassa": "ЮKassa",
    "cryptobot": "Криптовалюта",
    "telegram-stars": "Telegram Stars",
}


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Тарифы", callback_data="plans:list")],
            [InlineKeyboardButton(text="Мои подписки", callback_data="subs:mine")],
            [
                InlineKeyboardButton(text="Обучение Upscale", callback_data="edu:menu:-")
                if get_settings().enable_builtin_education
                else InlineKeyboardButton(text="Методичка", url="https://t.me/propmethods")
            ],
            [InlineKeyboardButton(text="Промокод", callback_data="promo:enter")],
            [InlineKeyboardButton(text="Помощь", callback_data="menu:help")],
            [InlineKeyboardButton(text="Документы", callback_data="legal:menu")],
        ]
    )


def plans_keyboard(plans: list[Plan]) -> InlineKeyboardMarkup:
    rows = []
    for plan in plans:
        channel_title = plan.channel.title if plan.channel else "Канал"
        rows.append(
            [
                InlineKeyboardButton(
                    text=(f"{channel_title} | {plan.title} | {plan_price_text(plan)}")[:64],
                    callback_data=f"plan:{plan.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="В меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_providers_keyboard(
    plan: Plan,
    promo_id: int | None = None,
) -> InlineKeyboardMarkup:
    settings = get_settings()
    providers: list[str] = []
    if settings.enable_telegram_stars and plan.stars_price:
        providers.append("telegram-stars")
    if settings.enable_cryptobot and settings.cryptobot_token and plan.currency.upper() != "XTR":
        providers.append("cryptobot")
    rows = [
        [
            InlineKeyboardButton(
                text=PAYMENT_PROVIDERS[provider],
                callback_data=_payment_callback(plan.id, provider, promo_id),
            )
        ]
        for provider in providers
    ]
    if not any((button.callback_data or "").startswith("pay:") for row in rows for button in row):
        rows.append([InlineKeyboardButton(text="Помощь", callback_data="menu:help")])
    rows.append([InlineKeyboardButton(text="К тарифам", callback_data="plans:list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def legal_acceptance_keyboard(
    plan_id: int,
    provider: str,
    promo_id: int | None = None,
) -> InlineKeyboardMarkup:
    context = f"{plan_id}:{provider}"
    if promo_id is not None:
        context += f":{promo_id}"
    rows = [
        [
            InlineKeyboardButton(
                text="Условия",
                callback_data=f"legaldoc:terms:0:{context}",
            )
        ],
        [
            InlineKeyboardButton(
                text="Обработка данных",
                callback_data=f"legaldoc:privacy:0:{context}",
            )
        ],
        [
            InlineKeyboardButton(
                text="Возвраты",
                callback_data=f"legaldoc:refunds:0:{context}",
            )
        ],
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="Принимаю условия",
                callback_data=f"legalaccept:{context}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="Отказаться",
                callback_data="promo:enter" if promo_id is not None else f"plan:{plan_id}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_keyboard(
    invoice_url: str | None,
    payment_id: int,
    allow_dev_confirmation: bool = False,
) -> InlineKeyboardMarkup:
    rows = []
    if invoice_url:
        rows.append([InlineKeyboardButton(text="Перейти к оплате", url=invoice_url)])
    if allow_dev_confirmation:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Подтвердить тестовую оплату", callback_data=f"devpay:{payment_id}"
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="Проверить оплату", callback_data=f"payments:status:{payment_id}"
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="В меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plan_price_text(plan: Plan) -> str:
    settings = get_settings()
    if getattr(settings, "app_env", "development") == "production":
        prices = []
        if (
            settings.enable_cryptobot
            and settings.cryptobot_token
            and plan.currency.upper() != "XTR"
        ):
            prices.append(_format_price(plan.price, plan.currency))
        if settings.enable_telegram_stars and plan.stars_price:
            prices.append(_format_price(Decimal(plan.stars_price), "XTR"))
        return " / ".join(prices) or "Оплата временно недоступна"
    return _format_price(plan.price, plan.currency)


def _format_price(price: Decimal, currency: str) -> str:
    normalized = price.quantize(Decimal(1)) if price == price.to_integral() else price
    return f"{normalized} {currency}"


def _payment_callback(
    plan_id: int,
    provider: str,
    promo_id: int | None,
) -> str:
    value = f"pay:{plan_id}:{provider}"
    return f"{value}:{promo_id}" if promo_id is not None else value
