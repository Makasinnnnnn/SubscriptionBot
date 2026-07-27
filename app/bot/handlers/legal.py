from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.keyboards.subscription import legal_acceptance_keyboard
from app.db.session import async_session_factory
from app.services.support_service import SupportContactService

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
    await message.answer(
        _support_menu_text(),
        reply_markup=_support_menu_keyboard(),
    )


@router.callback_query(F.data.in_({"menu:help", "support:menu"}))
async def support_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            _support_menu_text(),
            reply_markup=_support_menu_keyboard(),
        )


@router.callback_query(F.data == "support:faq")
async def support_faq(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "Частые вопросы\n\n"
            "• Оплата подтверждается автоматически.\n"
            "• После оплаты бот выдаёт ссылки в оба канала.\n"
            "• Если вы вышли из канала при активной подписке, откройте "
            "«Мои подписки» → «Получить доступ снова».\n"
            "• Если Telegram не принимает заявку, проверьте, что открыта самая "
            "свежая ссылка.",
            reply_markup=_support_back_keyboard(),
        )


@router.callback_query(F.data == "support:payment")
async def support_payment_issue(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "Проблема с оплатой\n\n"
            "Подготовьте Telegram ID, способ оплаты, примерное время и номер "
            "платежа. Не отправляйте пароль, код подтверждения или данные карты.",
            reply_markup=_support_issue_keyboard(),
        )


@router.callback_query(F.data == "support:access")
async def support_access_issue(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "Проблема со входом\n\n"
            "Сначала откройте «Мои подписки» и запросите доступ повторно. "
            "Если заявка не подтверждается, отправьте поддержке Telegram ID "
            "и название канала.",
            reply_markup=_support_issue_keyboard(),
        )


@router.callback_query(F.data == "support:contacts")
async def support_contacts(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is None:
        return
    contacts = await _support_targets(callback.bot)
    text = (
        "Выберите контакт поддержки:"
        if contacts
        else "Контакт поддержки пока не настроен."
    )
    await callback.message.edit_text(
        text,
        reply_markup=_support_keyboard(contacts, back_callback="support:menu"),
    )


@router.message(Command("paysupport"))
async def payment_support(message: Message) -> None:
    contacts = await _support_targets(message.bot)
    contact = ", ".join(label for label, _ in contacts) or "не настроен"
    await message.answer(
        f"Поддержка по платежам: {contact}\n\n"
        "Telegram не обрабатывает обращения по покупкам в этом боте. "
        "По вопросам оплаты и возврата обращайтесь к владельцу PropGate.",
        reply_markup=_support_keyboard(contacts, back_callback="menu:main"),
    )


async def _support_targets(bot: Bot) -> list[tuple[str, str]]:
    async with async_session_factory() as session:
        contacts = await SupportContactService(session).resolved(bot)
    return [(contact.label, contact.url) for contact in contacts]


def _support_keyboard(
    contacts: list[tuple[str, str]],
    *,
    back_callback: str,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            *[
                [InlineKeyboardButton(text=label, url=url)]
                for label, url in contacts
            ],
            [InlineKeyboardButton(text="Назад", callback_data=back_callback)],
        ]
    )


def _support_menu_text() -> str:
    return "Помощь PropGate\n\nВыберите тему обращения:"


def _support_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Частые вопросы", callback_data="support:faq")],
            [
                InlineKeyboardButton(
                    text="Проблема с оплатой",
                    callback_data="support:payment",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Проблема со входом",
                    callback_data="support:access",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Связаться с поддержкой",
                    callback_data="support:contacts",
                )
            ],
            [InlineKeyboardButton(text="Назад", callback_data="menu:main")],
        ]
    )


def _support_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data="support:menu")],
        ]
    )


def _support_issue_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Связаться с поддержкой",
                    callback_data="support:contacts",
                )
            ],
            [InlineKeyboardButton(text="Назад", callback_data="support:menu")],
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
