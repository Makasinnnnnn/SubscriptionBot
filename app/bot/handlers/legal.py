from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.keyboards.subscription import legal_acceptance_keyboard
from app.core.config import get_settings

router = Router()

LEGAL_DIR = Path(__file__).resolve().parents[3] / "legal"
DOCUMENTS = {
    "terms": ("Условия использования", LEGAL_DIR / "terms.md"),
    "privacy": ("Обработка данных", LEGAL_DIR / "privacy.md"),
    "refunds": ("Правила возврата", LEGAL_DIR / "refunds.md"),
}
PAGE_SIZE = 3000


@router.message(Command("terms"))
async def terms(message: Message) -> None:
    await _send_document(message, "terms")


@router.message(Command("privacy"))
async def privacy(message: Message) -> None:
    await _send_document(message, "privacy")


@router.message(Command("refund_policy"))
async def refund_policy(message: Message) -> None:
    await _send_document(message, "refunds")


@router.callback_query(F.data == "legal:menu")
async def legal_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "Документы PropGate\n\nВыберите документ:",
            reply_markup=_documents_keyboard(),
        )


@router.callback_query(F.data.startswith("legaldoc:"))
async def legal_document(callback: CallbackQuery) -> None:
    if callback.data is None or callback.message is None:
        return
    parts = callback.data.split(":")
    if len(parts) not in {4, 5}:
        await callback.answer("Некорректная ссылка.", show_alert=True)
        return
    key = parts[1]
    try:
        page = int(parts[2])
    except ValueError:
        await callback.answer("Некорректная страница.", show_alert=True)
        return
    context = parts[3:]
    await callback.answer()
    await _edit_document(callback, key, page, context)


@router.callback_query(F.data.startswith("legalreview:"))
async def legal_review(callback: CallbackQuery) -> None:
    if callback.data is None or callback.message is None:
        return
    parts = callback.data.split(":", maxsplit=2)
    if len(parts) != 3:
        return
    try:
        plan_id = int(parts[1])
    except ValueError:
        return
    await callback.answer()
    await callback.message.edit_text(
        "Перед оплатой ознакомьтесь с документами и подтвердите согласие с их текущей версией.",
        reply_markup=legal_acceptance_keyboard(plan_id, parts[2]),
    )


@router.message(Command("support"))
async def support(message: Message) -> None:
    contacts = await _support_targets(message.bot)
    await message.answer(
        "Выберите контакт поддержки:" if contacts else "Контакт поддержки пока не настроен.",
        reply_markup=_support_keyboard(contacts) if contacts else None,
    )


@router.message(Command("paysupport"))
async def payment_support(message: Message) -> None:
    contacts = await _support_targets(message.bot)
    contact = ", ".join(label for label, _ in contacts) or "не настроен"
    await message.answer(
        f"Поддержка по платежам: {contact}\n\n"
        "Telegram не обрабатывает обращения по покупкам в этом боте. "
        "По вопросам оплаты и возврата обращайтесь к владельцу PropGate.",
        reply_markup=_support_keyboard(contacts) if contacts else None,
    )


async def _support_targets(bot: Bot) -> list[tuple[str, str]]:
    settings = get_settings()
    contacts: list[tuple[str, str]] = []
    for telegram_id in settings.support_ids:
        try:
            chat = await bot.get_chat(telegram_id)
        except TelegramAPIError:
            continue
        if chat.username:
            target = (f"@{chat.username}", f"https://t.me/{chat.username}")
            if target not in contacts:
                contacts.append(target)

    raw_contacts = settings.support_contacts or [settings.support_contact]
    for raw in raw_contacts:
        for value in raw.replace(";", ",").split(","):
            username = value.strip().lstrip("@")
            target = (f"@{username}", f"https://t.me/{username}")
            if username and target not in contacts:
                contacts.append(target)
    return contacts


def _support_keyboard(contacts: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, url=url)]
            for label, url in contacts
        ]
    )


async def _send_document(message: Message, key: str) -> None:
    title, _ = DOCUMENTS[key]
    pages = _document_pages(key)
    await message.answer(
        _page_text(title, pages, 0),
        reply_markup=_document_keyboard(key, 0, len(pages), ["menu"]),
    )


async def _edit_document(
    callback: CallbackQuery,
    key: str,
    page: int,
    context: list[str],
) -> None:
    if key not in DOCUMENTS or callback.message is None:
        await callback.answer("Документ не найден.", show_alert=True)
        return
    title, _ = DOCUMENTS[key]
    pages = _document_pages(key)
    page = max(0, min(page, len(pages) - 1))
    await callback.message.edit_text(
        _page_text(title, pages, page),
        reply_markup=_document_keyboard(key, page, len(pages), context),
    )


def _documents_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Условия использования", callback_data="legaldoc:terms:0:menu")],
            [InlineKeyboardButton(text="Обработка данных", callback_data="legaldoc:privacy:0:menu")],
            [InlineKeyboardButton(text="Правила возврата", callback_data="legaldoc:refunds:0:menu")],
            [InlineKeyboardButton(text="В меню", callback_data="menu:main")],
        ]
    )


def _document_keyboard(
    key: str,
    page: int,
    page_count: int,
    context: list[str],
) -> InlineKeyboardMarkup:
    suffix = ":".join(context)
    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(text="Назад", callback_data=f"legaldoc:{key}:{page - 1}:{suffix}")
        )
    if page + 1 < page_count:
        navigation.append(
            InlineKeyboardButton(text="Далее", callback_data=f"legaldoc:{key}:{page + 1}:{suffix}")
        )
    rows = [navigation] if navigation else []
    if context == ["menu"]:
        rows.append([InlineKeyboardButton(text="К документам", callback_data="legal:menu")])
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="К подтверждению",
                    callback_data=f"legalreview:{context[0]}:{context[1]}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _document_pages(key: str) -> list[str]:
    _, path = DOCUMENTS[key]
    text = _plain_markdown(path.read_text(encoding="utf-8"))
    paragraphs = [paragraph.strip() for paragraph in text.split("\n\n") if paragraph.strip()]
    pages: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip()
        if current and len(candidate) > PAGE_SIZE:
            pages.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        pages.append(current)
    return pages or ["Документ пока не опубликован."]


def _plain_markdown(text: str) -> str:
    return "\n".join(line.lstrip("# ") if line.startswith("#") else line for line in text.splitlines())


def _page_text(title: str, pages: list[str], page: int) -> str:
    counter = f"\n\nСтраница {page + 1} из {len(pages)}" if len(pages) > 1 else ""
    return f"{title}\n\n{pages[page]}{counter}"
