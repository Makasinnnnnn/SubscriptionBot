from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.education.content import LESSONS, MEDIA, TITLES


def button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def menu(signal_id: str = "-") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button(title, f"edu:page:{key}:0:{signal_id}")] for key, title in TITLES.items()
        ]
        + [
            [button("Рассчитать риск", f"edu:calc:{signal_id}")],
            [button("Главное меню", "menu:main")],
        ]
    )


def navigation(key: str, page: int, signal_id: str = "-") -> InlineKeyboardMarkup:
    row = []
    if page:
        row.append(button("Назад", f"edu:page:{key}:{page - 1}:{signal_id}"))
    if page + 1 < len(LESSONS[key]):
        row.append(button("Далее", f"edu:page:{key}:{page + 1}:{signal_id}"))
    return InlineKeyboardMarkup(
        inline_keyboard=([row] if row else [])
        + [
            [button(f"Скриншот {index + 1}", f"edu:photo:{name}.png")]
            for index, name in enumerate(MEDIA.get((key, page), []))
        ]
        + [
            [button("К списку инструкций", f"edu:menu:{signal_id}")],
            [button("Рассчитать риск", f"edu:calc:{signal_id}")],
            [InlineKeyboardButton(text="Открыть Upscale", url="https://app.upscale.trade")],
        ]
    )
