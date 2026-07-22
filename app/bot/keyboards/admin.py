from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Тарифы", callback_data="adm:plans"),
                InlineKeyboardButton(text="Доступ", callback_data="adm:access"),
            ],
            [
                InlineKeyboardButton(text="Каналы", callback_data="admin:channels"),
                InlineKeyboardButton(text="Платежи", callback_data="admin:payments"),
            ],
            [
                InlineKeyboardButton(text="Подписки", callback_data="admin:subscriptions"),
                InlineKeyboardButton(text="Статистика", callback_data="admin:stats"),
            ],
            [InlineKeyboardButton(text="Telegram Stars", callback_data="adm:stars")],
            [InlineKeyboardButton(text="Уведомления", callback_data="adm:notifications")],
            [InlineKeyboardButton(text="Администраторы", callback_data="admin:admins")],
        ]
    )


def admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data="admin:menu")],
        ]
    )


def admin_cancel_keyboard(back_callback: str = "admin:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data=back_callback)],
        ]
    )


def refund_confirmation_keyboard(payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подтвердить возврат",
                    callback_data=f"adminrefund:{payment_id}",
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="admin:menu")],
        ]
    )
