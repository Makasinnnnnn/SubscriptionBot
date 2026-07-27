from pathlib import Path
from datetime import UTC, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.admin.service import AdminPermissionService
from app.bot.keyboards.subscription import (
    legal_acceptance_keyboard,
    main_menu_keyboard,
)
from app.db.session import async_session_factory
from app.db.models import AuditLog
from app.services.support_service import (
    ResolvedSupportContact,
    SupportContact,
    SupportContactService,
)

router = Router()

LEGAL_DIR = Path(__file__).resolve().parents[3] / "legal"
DOCUMENTS = {
    "terms": ("Условия использования", LEGAL_DIR / "terms.md"),
    "privacy": ("Обработка данных", LEGAL_DIR / "privacy.md"),
    "refunds": ("Правила возврата", LEGAL_DIR / "refunds.md"),
}
PAGE_SIZE = 3000
SUPPORT_MESSAGE_COOLDOWN = timedelta(minutes=1)


class SupportMessageForm(StatesGroup):
    message = State()


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
    if len(parts) not in {4, 5, 6}:
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
    parts = callback.data.split(":")
    if len(parts) not in {3, 4}:
        return
    try:
        plan_id = int(parts[1])
    except ValueError:
        return
    promo_id = None
    if len(parts) == 4:
        try:
            promo_id = int(parts[3])
        except ValueError:
            return
    await callback.answer()
    await callback.message.edit_text(
        "Перед оплатой ознакомьтесь с документами и подтвердите согласие с их текущей версией.",
        reply_markup=legal_acceptance_keyboard(plan_id, parts[2], promo_id),
    )


@router.message(Command("support"))
async def support(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        _support_menu_text(),
        reply_markup=_support_menu_keyboard(),
    )


@router.callback_query(F.data.in_({"menu:help", "support:menu"}))
async def support_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
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


@router.callback_query(
    (F.data == "support:write") | F.data.startswith("support:write:")
)
async def support_write_start(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if callback.message is None:
        return
    await callback.answer()
    contact = await _support_relay_target()
    if contact is None:
        await callback.message.edit_text(
            "Получатель обращений пока не настроен. "
            "Используйте прямой контакт поддержки.",
            reply_markup=_support_back_keyboard(),
        )
        return
    await state.set_state(SupportMessageForm.message)
    await state.update_data(
        support_kind=contact.kind,
        support_value=contact.value,
        support_label=contact.label,
    )
    await callback.message.edit_text(
        "Обращение в поддержку\n\n"
        "Напишите одним сообщением, что произошло. Бот передаст текст вместе "
        "с вашим Telegram ID. Не отправляйте пароли, коды или данные карты.",
        reply_markup=_support_back_keyboard(),
    )


@router.message(
    SupportMessageForm.message,
    F.text,
    ~F.text.startswith("/"),
)
async def support_write_send(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        await state.clear()
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Отправьте обращение текстовым сообщением.")
        return
    if len(text) > 3000:
        await message.answer("Сообщение слишком длинное. Сократите его до 3000 символов.")
        return
    data = await state.get_data()
    target_value = str(data.get("support_value", ""))
    now = datetime.now(UTC).replace(tzinfo=None)
    async with async_session_factory() as session:
        definitions = await SupportContactService(session).definitions()
        last_sent_at = await session.scalar(
            select(AuditLog.created_at)
            .where(
                AuditLog.actor_telegram_id == message.from_user.id,
                AuditLog.action == "support.message.sent",
            )
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
    allowed = any(
        contact.kind == "telegram_id" and contact.value == target_value
        for contact in definitions
    )
    if not allowed:
        await state.clear()
        await message.answer(
            "Контакт поддержки изменился. Откройте раздел поддержки ещё раз.",
            reply_markup=_support_menu_keyboard(),
        )
        return
    if last_sent_at and now - last_sent_at < SUPPORT_MESSAGE_COOLDOWN:
        wait_seconds = max(
            1,
            int((SUPPORT_MESSAGE_COOLDOWN - (now - last_sent_at)).total_seconds()),
        )
        await message.answer(
            f"Предыдущее обращение уже доставлено. Новое можно отправить через "
            f"{wait_seconds} сек."
        )
        return
    username = (
        f"@{message.from_user.username}"
        if message.from_user.username
        else "не задан"
    )
    try:
        await message.bot.send_message(
            int(target_value),
            "Новое обращение в поддержку PropGate\n\n"
            f"Пользователь: {username}\n"
            f"Telegram ID: {message.from_user.id}\n"
            f"Имя: {message.from_user.full_name}\n\n"
            f"{text}",
        )
    except (TelegramAPIError, ValueError):
        await state.clear()
        await message.answer(
            "Не удалось доставить сообщение. Сотрудник поддержки должен сначала "
            "запустить этого бота командой /start.",
            reply_markup=_support_menu_keyboard(),
        )
        return
    async with async_session_factory() as session:
        await AdminPermissionService(session).audit(
            message.from_user.id,
            "support.message.sent",
            "support_contact",
            target_value,
            {"label": str(data.get("support_label", ""))[:48]},
        )
        await session.commit()
    await state.clear()
    await message.answer(
        "Сообщение доставлено поддержке.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("paysupport"))
async def payment_support(message: Message) -> None:
    contacts = await _support_targets(message.bot)
    contact = ", ".join(item.label for item in contacts) or "не настроен"
    await message.answer(
        f"Поддержка по платежам: {contact}\n\n"
        "Telegram не обрабатывает обращения по покупкам в этом боте. "
        "По вопросам оплаты и возврата обращайтесь к владельцу PropGate.",
        reply_markup=_support_keyboard(contacts, back_callback="menu:main"),
    )


async def _support_targets(bot: Bot) -> list[ResolvedSupportContact]:
    async with async_session_factory() as session:
        return await SupportContactService(session).resolved(bot)


async def _support_relay_target() -> SupportContact | None:
    async with async_session_factory() as session:
        return await SupportContactService(session).relay_target()


def _support_keyboard(
    contacts: list[ResolvedSupportContact],
    *,
    back_callback: str,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    seen_urls: set[str] = set()
    for contact in contacts:
        if not contact.url:
            continue
        normalized_url = contact.url.lower().rstrip("/")
        if normalized_url in seen_urls:
            continue
        seen_urls.add(normalized_url)
        rows.append([InlineKeyboardButton(text=contact.label, url=contact.url)])
    if any(contact.source.kind == "telegram_id" for contact in contacts):
        rows.append(
            [
                InlineKeyboardButton(
                    text="Оставить обращение в поддержку",
                    callback_data="support:write",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(
        inline_keyboard=rows
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
                    callback_data=f"legalreview:{':'.join(context)}",
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
