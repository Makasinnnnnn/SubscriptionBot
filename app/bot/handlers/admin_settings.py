from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.admin.service import AdminPermissionService
from app.admin.settings_service import RuntimeSettingsService
from app.bot.keyboards.admin import admin_cancel_keyboard, admin_menu_keyboard
from app.core.config import get_settings
from app.db.enums import PaymentStatus
from app.db.models import Payment, Plan, PromoCode, PromoRedemption
from app.db.session import async_session_factory
from app.services.promo_service import PromoRedemptionError, normalize_promo_code
from app.services.support_service import (
    MAX_SUPPORT_CONTACTS,
    SupportContact,
    SupportContactService,
    normalize_support_contact,
)

router = Router()


class PromoAdminForm(StatesGroup):
    code = State()
    discount_percent = State()
    valid_from = State()
    valid_until = State()
    max_redemptions = State()
    edit_value = State()


class SupportAdminForm(StatesGroup):
    value = State()
    label = State()
    edit_label = State()


class ReminderAdminForm(StatesGroup):
    intervals = State()


@router.callback_query(F.data.startswith("cfg:promos:"))
async def promo_list(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    filter_token = (callback.data or "").rsplit(":", 1)[-1]
    if filter_token not in {"active", "archive"}:
        return
    async with async_session_factory() as session:
        scope = await _manage_scope(session, callback.from_user.id)
        if scope is _DENIED:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        statement = (
            select(PromoCode)
            .join(PromoCode.plan)
            .options(selectinload(PromoCode.plan).selectinload(Plan.channel))
            .where(PromoCode.is_active.is_(filter_token == "active"))
            .order_by(PromoCode.id.desc())
        )
        if scope is not None:
            statement = (
                statement.where(Plan.channel_id.in_(scope))
                if scope
                else statement.where(False)
            )
        promos = list(await session.scalars(statement))
    await callback.answer()
    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"{promo.code} | -{promo.discount_percent}% | "
                    f"{promo.redemptions_count}/"
                    f"{promo.max_redemptions} | {promo.plan.title}"
                )[:64],
                callback_data=f"cfg:promo:{promo.id}",
            )
        ]
        for promo in promos[:50]
    ]
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text=f"{'✓ ' if filter_token == 'active' else ''}Активные",
                    callback_data="cfg:promos:active",
                ),
                InlineKeyboardButton(
                    text=f"{'✓ ' if filter_token == 'archive' else ''}Архив",
                    callback_data="cfg:promos:archive",
                ),
            ],
            [InlineKeyboardButton(text="Создать", callback_data="cfg:promocreate")],
            [InlineKeyboardButton(text="В меню", callback_data="admin:menu")],
        ]
    )
    await _edit(
        callback,
        f"Промокоды: {'активные' if filter_token == 'active' else 'архив'}\n"
        f"Найдено: {len(promos)}",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "cfg:promocreate")
async def promo_create_start(callback: CallbackQuery, state: FSMContext) -> None:
    async with async_session_factory() as session:
        plans = await _managed_active_plans(session, callback.from_user.id)
    if not plans:
        await callback.answer("Нет доступных активных тарифов.", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await _edit(
        callback,
        "Новый промокод\n\nВыберите тариф:",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"{plan.channel.title} | {plan.title}"[:64],
                        callback_data=f"cfg:promoplan:{plan.id}",
                    )
                ]
                for plan in plans
            ]
            + [[InlineKeyboardButton(text="Отмена", callback_data="cfg:promos:active")]]
        ),
    )


@router.callback_query(F.data.startswith("cfg:promoplan:"))
async def promo_create_plan(callback: CallbackQuery, state: FSMContext) -> None:
    plan_id = _callback_int(callback, 2)
    if plan_id is None:
        return
    async with async_session_factory() as session:
        plan = await _managed_plan(session, callback.from_user.id, plan_id)
    if plan is None or not plan.is_active or not plan.channel.is_active:
        await callback.answer("Тариф недоступен.", show_alert=True)
        return
    await state.set_state(PromoAdminForm.code)
    await state.update_data(plan_id=plan.id)
    await callback.answer()
    await _edit(
        callback,
        f"Выбран тариф: {plan.channel.title} | {plan.title}\n\n"
        "Введите код: 4-32 латинских символа, цифры, '-' или '_'.",
        admin_cancel_keyboard("cfg:promos:active"),
    )


@router.message(PromoAdminForm.code)
async def promo_create_code(message: Message, state: FSMContext) -> None:
    if not await _can_manage_message(message):
        await state.clear()
        return
    try:
        code = normalize_promo_code(message.text or "")
    except PromoRedemptionError as exc:
        await message.answer(str(exc))
        return
    async with async_session_factory() as session:
        duplicate = await session.scalar(
            select(PromoCode.id).where(PromoCode.code == code)
        )
    if duplicate is not None:
        await message.answer("Такой промокод уже существует.")
        return
    await state.update_data(code=code)
    await state.set_state(PromoAdminForm.discount_percent)
    await message.answer(
        "Введите скидку в процентах: целое число от 1 до 99."
    )


@router.message(PromoAdminForm.discount_percent)
async def promo_create_discount(message: Message, state: FSMContext) -> None:
    try:
        discount_percent = _bounded_int(message.text, 1, 99)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(discount_percent=discount_percent)
    await state.set_state(PromoAdminForm.valid_from)
    await message.answer(
        "Дата начала в UTC: `сейчас` или ДД.ММ.ГГГГ ЧЧ:ММ.",
        parse_mode="Markdown",
        reply_markup=admin_cancel_keyboard("cfg:promos:active"),
    )


@router.message(PromoAdminForm.valid_from)
async def promo_create_from(message: Message, state: FSMContext) -> None:
    try:
        valid_from = _parse_utc_datetime(message.text or "", allow_now=True)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(valid_from=valid_from.isoformat())
    await state.set_state(PromoAdminForm.valid_until)
    await message.answer("Дата окончания в UTC: ДД.ММ.ГГГГ ЧЧ:ММ.")


@router.message(PromoAdminForm.valid_until)
async def promo_create_until(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        valid_until = _parse_utc_datetime(message.text or "")
        valid_from = datetime.fromisoformat(data["valid_from"])
        if valid_until <= valid_from:
            raise ValueError("Дата окончания должна быть позже даты начала.")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(valid_until=valid_until.isoformat())
    await state.set_state(PromoAdminForm.max_redemptions)
    await message.answer("Введите общий лимит применений: от 1 до 1 000 000.")


@router.message(PromoAdminForm.max_redemptions)
async def promo_create_limit(message: Message, state: FSMContext) -> None:
    try:
        limit = _bounded_int(message.text, 1, 1_000_000)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    if message.from_user is None:
        return
    data = await state.get_data()
    async with async_session_factory() as session:
        plan = await _managed_plan(
            session,
            message.from_user.id,
            int(data["plan_id"]),
        )
        if plan is None or not plan.is_active or not plan.channel.is_active:
            await state.clear()
            await message.answer("Тариф больше недоступен.")
            return
        promo = PromoCode(
            code=data["code"],
            plan_id=plan.id,
            valid_from=datetime.fromisoformat(data["valid_from"]),
            valid_until=datetime.fromisoformat(data["valid_until"]),
            discount_percent=int(data["discount_percent"]),
            max_redemptions=limit,
            per_user_limit=1,
            created_by_telegram_id=message.from_user.id,
        )
        session.add(promo)
        await session.flush()
        await AdminPermissionService(session).audit(
            message.from_user.id,
            "promo.create",
            "promo_code",
            promo.id,
            {
                "plan_id": plan.id,
                "discount_percent": promo.discount_percent,
                "max_redemptions": limit,
            },
        )
        await session.commit()
        promo_id = promo.id
    await state.clear()
    await message.answer(
        "Промокод создан.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть карточку",
                        callback_data=f"cfg:promo:{promo_id}",
                    )
                ]
            ]
        ),
    )


@router.callback_query(F.data.startswith("cfg:promo:"))
async def promo_card(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    promo_id = _callback_int(callback, 2)
    if promo_id is None:
        return
    async with async_session_factory() as session:
        promo = await _managed_promo(session, callback.from_user.id, promo_id)
        if promo is None:
            await callback.answer("Промокод недоступен.", show_alert=True)
            return
        recent = list(
            await session.scalars(
                select(PromoRedemption)
                .options(selectinload(PromoRedemption.user))
                .where(PromoRedemption.promo_code_id == promo.id)
                .order_by(PromoRedemption.redeemed_at.desc())
                .limit(10)
            )
        )
    await callback.answer()
    recent_lines = [
        (
            f"{item.redeemed_at:%d.%m %H:%M} UTC — "
            f"{'@' + item.user.username if item.user.username else item.user.telegram_id}"
        )
        for item in recent
    ]
    text = (
        f"Промокод {promo.code}\n\n"
        f"Статус: {'активен' if promo.is_active else 'архив'}\n"
        f"Тариф: {promo.plan.title}\n"
        f"Канал: {promo.plan.channel.title}\n"
        f"Скидка: {promo.discount_percent}%\n"
        f"Начало: {promo.valid_from:%d.%m.%Y %H:%M} UTC\n"
        f"Окончание: {promo.valid_until:%d.%m.%Y %H:%M} UTC\n"
        f"Использовано: {promo.redemptions_count}/{promo.max_redemptions}\n"
        f"Лимит на пользователя: {promo.per_user_limit}\n\n"
        "Последние применения:\n"
        + ("\n".join(recent_lines) if recent_lines else "нет")
    )
    rows = [
        [
            InlineKeyboardButton(
                text="Скидка",
                callback_data=f"cfg:promoedit:{promo.id}:discount",
            ),
            InlineKeyboardButton(
                text="Тариф",
                callback_data=f"cfg:promoeditplan:{promo.id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="Лимит",
                callback_data=f"cfg:promoedit:{promo.id}:limit",
            ),
        ],
        [
            InlineKeyboardButton(
                text="Дата начала",
                callback_data=f"cfg:promoedit:{promo.id}:from",
            ),
            InlineKeyboardButton(
                text="Дата окончания",
                callback_data=f"cfg:promoedit:{promo.id}:until",
            ),
        ],
        [
            InlineKeyboardButton(
                text="Архивировать" if promo.is_active else "Включить",
                callback_data=f"cfg:promotoggleask:{promo.id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="К списку",
                callback_data=(
                    "cfg:promos:active" if promo.is_active else "cfg:promos:archive"
                ),
            )
        ],
    ]
    await _edit(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("cfg:promoeditplan:"))
async def promo_edit_plan_start(callback: CallbackQuery) -> None:
    promo_id = _callback_int(callback, 2)
    if promo_id is None:
        return
    async with async_session_factory() as session:
        promo = await _managed_promo(session, callback.from_user.id, promo_id)
        plans = await _managed_active_plans(session, callback.from_user.id)
    if promo is None:
        await callback.answer("Промокод недоступен.", show_alert=True)
        return
    await callback.answer()
    await _edit(
        callback,
        "Выберите новый тариф:",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"{plan.channel.title} | {plan.title}"[:64],
                        callback_data=f"cfg:promosetplan:{promo_id}:{plan.id}",
                    )
                ]
                for plan in plans
            ]
            + [
                [
                    InlineKeyboardButton(
                        text="Отмена",
                        callback_data=f"cfg:promo:{promo_id}",
                    )
                ]
            ]
        ),
    )


@router.callback_query(F.data.startswith("cfg:promosetplan:"))
async def promo_edit_plan_save(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        return
    try:
        promo_id, plan_id = int(parts[2]), int(parts[3])
    except ValueError:
        return
    async with async_session_factory() as session:
        promo = await _managed_promo(session, callback.from_user.id, promo_id)
        plan = await _managed_plan(session, callback.from_user.id, plan_id)
        if (
            promo is None
            or plan is None
            or not plan.is_active
            or not plan.channel.is_active
        ):
            await callback.answer("Изменение недоступно.", show_alert=True)
            return
        if promo.redemptions_count > 0 or await _promo_locked_payment_count(
            session,
            promo.id,
        ):
            await callback.answer(
                "Тариф нельзя менять после создания счетов по промокоду.",
                show_alert=True,
            )
            return
        old_plan_id = promo.plan_id
        promo.plan_id = plan.id
        await AdminPermissionService(session).audit(
            callback.from_user.id,
            "promo.plan.update",
            "promo_code",
            promo.id,
            {"old_plan_id": old_plan_id, "new_plan_id": plan.id},
        )
        await session.commit()
    await promo_card(callback, _empty_state())


@router.callback_query(F.data.startswith("cfg:promoedit:"))
async def promo_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 4 or parts[3] not in {
        "discount",
        "limit",
        "from",
        "until",
    }:
        return
    try:
        promo_id = int(parts[2])
    except ValueError:
        return
    async with async_session_factory() as session:
        promo = await _managed_promo(session, callback.from_user.id, promo_id)
    if promo is None:
        await callback.answer("Промокод недоступен.", show_alert=True)
        return
    prompts = {
        "discount": "Новая скидка: целое число от 1 до 99.",
        "limit": "Новый общий лимит применений:",
        "from": "Новая дата начала в UTC: `сейчас` или ДД.ММ.ГГГГ ЧЧ:ММ.",
        "until": "Новая дата окончания в UTC: ДД.ММ.ГГГГ ЧЧ:ММ.",
    }
    await state.set_state(PromoAdminForm.edit_value)
    await state.update_data(promo_id=promo.id, field=parts[3])
    await callback.answer()
    await _edit(
        callback,
        prompts[parts[3]],
        admin_cancel_keyboard(f"cfg:promo:{promo.id}"),
    )


@router.message(PromoAdminForm.edit_value)
async def promo_edit_save(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    promo_id = int(data["promo_id"])
    field = data["field"]
    async with async_session_factory() as session:
        promo = await _managed_promo(session, message.from_user.id, promo_id)
        if promo is None:
            await state.clear()
            await message.answer("Промокод недоступен.")
            return
        try:
            if field == "discount":
                if await _promo_locked_payment_count(session, promo.id):
                    raise ValueError(
                        "Скидку нельзя менять после создания счетов по промокоду."
                    )
                value = _bounded_int(message.text, 1, 99)
                old_value = promo.discount_percent
                promo.discount_percent = value
            elif field == "limit":
                value = _bounded_int(message.text, 1, 1_000_000)
                if value < promo.redemptions_count:
                    raise ValueError(
                        "Лимит не может быть меньше уже использованного количества."
                    )
                old_value = promo.max_redemptions
                promo.max_redemptions = value
            else:
                value = _parse_utc_datetime(
                    message.text or "",
                    allow_now=field == "from",
                )
                if field == "from":
                    if value >= promo.valid_until:
                        raise ValueError("Дата начала должна быть раньше окончания.")
                    old_value = promo.valid_from.isoformat()
                    promo.valid_from = value
                else:
                    if value <= promo.valid_from:
                        raise ValueError("Дата окончания должна быть позже начала.")
                    old_value = promo.valid_until.isoformat()
                    promo.valid_until = value
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await AdminPermissionService(session).audit(
            message.from_user.id,
            f"promo.{field}.update",
            "promo_code",
            promo.id,
            {"old": str(old_value), "new": str(value)},
        )
        await session.commit()
    await state.clear()
    await message.answer(
        "Изменение сохранено.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Вернуться к промокоду",
                        callback_data=f"cfg:promo:{promo_id}",
                    )
                ]
            ]
        ),
    )


@router.callback_query(F.data.startswith("cfg:promotoggleask:"))
async def promo_toggle_ask(callback: CallbackQuery) -> None:
    promo_id = _callback_int(callback, 2)
    if promo_id is None:
        return
    async with async_session_factory() as session:
        promo = await _managed_promo(session, callback.from_user.id, promo_id)
    if promo is None:
        await callback.answer("Промокод недоступен.", show_alert=True)
        return
    await callback.answer()
    action = "архивировать" if promo.is_active else "включить"
    await _edit(
        callback,
        f"Точно {action} промокод {promo.code}?",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Подтвердить",
                        callback_data=f"cfg:promotoggle:{promo.id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Отмена",
                        callback_data=f"cfg:promo:{promo.id}",
                    )
                ],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cfg:promotoggle:"))
async def promo_toggle(callback: CallbackQuery) -> None:
    promo_id = _callback_int(callback, 2)
    if promo_id is None:
        return
    async with async_session_factory() as session:
        promo = await _managed_promo(session, callback.from_user.id, promo_id)
        if promo is None:
            await callback.answer("Промокод недоступен.", show_alert=True)
            return
        if not promo.is_active and (
            promo.valid_until <= datetime.now(UTC).replace(tzinfo=None)
            or promo.valid_until <= promo.valid_from
            or not promo.plan.is_active
            or not promo.plan.channel.is_active
        ):
            await callback.answer(
                "Нельзя включить код: проверьте даты и активность тарифа.",
                show_alert=True,
            )
            return
        promo.is_active = not promo.is_active
        await AdminPermissionService(session).audit(
            callback.from_user.id,
            "promo.enable" if promo.is_active else "promo.archive",
            "promo_code",
            promo.id,
        )
        await session.commit()
    await promo_card(callback, _empty_state())


@router.callback_query(F.data == "cfg:support")
async def support_settings(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    async with async_session_factory() as session:
        if not AdminPermissionService(session).is_superadmin(callback.from_user.id):
            await callback.answer("Только для superadmin.", show_alert=True)
            return
        contacts = await SupportContactService(session).definitions()
    await callback.answer()
    rows = [
        [
            InlineKeyboardButton(
                text=f"{index + 1}. {contact.label}"[:64],
                callback_data=f"cfg:supportitem:{index}",
            )
        ]
        for index, contact in enumerate(contacts)
    ]
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="Добавить ID",
                    callback_data="cfg:supportadd:telegram_id",
                ),
                InlineKeyboardButton(
                    text="Добавить @username",
                    callback_data="cfg:supportadd:username",
                ),
            ],
            [InlineKeyboardButton(text="Вернуть .env", callback_data="cfg:supportresetask")],
            [InlineKeyboardButton(text="В меню", callback_data="admin:menu")],
        ]
    )
    await _edit(
        callback,
        f"Контакты поддержки\n\nНастроено: {len(contacts)}/{MAX_SUPPORT_CONTACTS}",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("cfg:supportitem:"))
async def support_item(callback: CallbackQuery) -> None:
    index = _callback_int(callback, 2)
    if index is None:
        return
    async with async_session_factory() as session:
        if not AdminPermissionService(session).is_superadmin(callback.from_user.id):
            await callback.answer("Только для superadmin.", show_alert=True)
            return
        contacts = await SupportContactService(session).definitions()
    if index >= len(contacts):
        await callback.answer("Контакт не найден.", show_alert=True)
        return
    contact = contacts[index]
    value = f"@{contact.value}" if contact.kind == "username" else contact.value
    await callback.answer()
    await _edit(
        callback,
        f"Контакт #{index + 1}\n\n"
        f"Тип: {contact.kind}\n"
        f"Значение: {value}\n"
        f"Подпись: {contact.label}",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Проверить",
                        callback_data=f"cfg:supportcheck:{index}",
                    ),
                    InlineKeyboardButton(
                        text="Подпись",
                        callback_data=f"cfg:supportlabel:{index}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="Вверх",
                        callback_data=f"cfg:supportmove:{index}:up",
                    ),
                    InlineKeyboardButton(
                        text="Вниз",
                        callback_data=f"cfg:supportmove:{index}:down",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="Удалить",
                        callback_data=f"cfg:supportdeleteask:{index}",
                    )
                ],
                [InlineKeyboardButton(text="Назад", callback_data="cfg:support")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cfg:supportadd:"))
async def support_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    kind = (callback.data or "").rsplit(":", 1)[-1]
    if kind not in {"telegram_id", "username"}:
        return
    if not await _superadmin(callback):
        return
    await state.set_state(SupportAdminForm.value)
    await state.update_data(kind=kind)
    await callback.answer()
    prompt = "Введите положительный Telegram ID:" if kind == "telegram_id" else "Введите @username:"
    await _edit(callback, prompt, admin_cancel_keyboard("cfg:support"))


@router.message(SupportAdminForm.value)
async def support_add_value(message: Message, state: FSMContext) -> None:
    if not await _superadmin_message(message):
        await state.clear()
        return
    data = await state.get_data()
    try:
        contact = normalize_support_contact(
            kind=data["kind"],
            value=message.text or "",
        )
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(value=contact.value)
    await state.set_state(SupportAdminForm.label)
    await message.answer("Введите подпись кнопки (до 48 символов):")


@router.message(SupportAdminForm.label)
async def support_add_label(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not await _superadmin_message(message):
        await state.clear()
        return
    data = await state.get_data()
    try:
        contact = normalize_support_contact(
            kind=data["kind"],
            value=data["value"],
            label=message.text or "",
        )
    except ValueError as exc:
        await message.answer(str(exc))
        return
    async with async_session_factory() as session:
        service = SupportContactService(session)
        contacts = await service.definitions()
        try:
            await service.save(contacts + [contact], message.from_user.id)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await AdminPermissionService(session).audit(
            message.from_user.id,
            "support.contact.add",
            "app_setting",
            metadata={"kind": contact.kind},
        )
        await session.commit()
    await state.clear()
    await message.answer("Контакт добавлен.", reply_markup=admin_menu_keyboard())


@router.callback_query(F.data.startswith("cfg:supportlabel:"))
async def support_label_start(callback: CallbackQuery, state: FSMContext) -> None:
    index = _callback_int(callback, 2)
    if index is None or not await _superadmin(callback):
        return
    await state.set_state(SupportAdminForm.edit_label)
    await state.update_data(index=index)
    await callback.answer()
    await _edit(
        callback,
        "Введите новую подпись (до 48 символов):",
        admin_cancel_keyboard(f"cfg:supportitem:{index}"),
    )


@router.message(SupportAdminForm.edit_label)
async def support_label_save(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not await _superadmin_message(message):
        await state.clear()
        return
    label = (message.text or "").strip()
    if not 1 <= len(label) <= 48:
        await message.answer("Подпись должна содержать 1-48 символов.")
        return
    data = await state.get_data()
    index = int(data["index"])
    async with async_session_factory() as session:
        service = SupportContactService(session)
        contacts = await service.definitions()
        if index >= len(contacts):
            await state.clear()
            await message.answer("Контакт не найден.")
            return
        current = contacts[index]
        contacts[index] = SupportContact(
            kind=current.kind,
            value=current.value,
            label=label,
            position=current.position,
        )
        await service.save(contacts, message.from_user.id)
        await AdminPermissionService(session).audit(
            message.from_user.id,
            "support.contact.label",
            "app_setting",
            metadata={"index": index},
        )
        await session.commit()
    await state.clear()
    await message.answer("Подпись изменена.", reply_markup=admin_menu_keyboard())


@router.callback_query(F.data.startswith("cfg:supportcheck:"))
async def support_check(callback: CallbackQuery) -> None:
    index = _callback_int(callback, 2)
    if index is None or not await _superadmin(callback):
        return
    async with async_session_factory() as session:
        contacts = await SupportContactService(session).definitions()
    if index >= len(contacts):
        await callback.answer("Контакт не найден.", show_alert=True)
        return
    contact = contacts[index]
    chat_ref = (
        f"@{contact.value}"
        if contact.kind == "username"
        else int(contact.value)
    )
    try:
        chat = await callback.bot.get_chat(chat_ref)
    except TelegramAPIError:
        await callback.answer(
            "Bot API не смог открыть контакт. Проверьте username/ID и доступ бота.",
            show_alert=True,
        )
        return
    username = f"@{chat.username}" if chat.username else "без публичного username"
    await callback.answer(f"Контакт доступен: {username}", show_alert=True)


@router.callback_query(F.data.startswith("cfg:supportmove:"))
async def support_move(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 4 or parts[3] not in {"up", "down"} or not await _superadmin(callback):
        return
    try:
        index = int(parts[2])
    except ValueError:
        return
    async with async_session_factory() as session:
        service = SupportContactService(session)
        contacts = await service.definitions()
        target = index - 1 if parts[3] == "up" else index + 1
        if index < 0 or index >= len(contacts) or target < 0 or target >= len(contacts):
            await callback.answer("Контакт уже находится у края списка.", show_alert=True)
            return
        contacts[index], contacts[target] = contacts[target], contacts[index]
        await service.save(contacts, callback.from_user.id)
        await AdminPermissionService(session).audit(
            callback.from_user.id,
            "support.contact.move",
            "app_setting",
            metadata={"from": index, "to": target},
        )
        await session.commit()
    await support_settings(callback, _empty_state())


@router.callback_query(F.data.startswith("cfg:supportdeleteask:"))
async def support_delete_ask(callback: CallbackQuery) -> None:
    index = _callback_int(callback, 2)
    if index is None or not await _superadmin(callback):
        return
    await callback.answer()
    await _edit(
        callback,
        "Удалить этот контакт поддержки?",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Удалить",
                        callback_data=f"cfg:supportdelete:{index}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Отмена",
                        callback_data=f"cfg:supportitem:{index}",
                    )
                ],
            ]
        ),
    )


@router.callback_query(F.data.startswith("cfg:supportdelete:"))
async def support_delete(callback: CallbackQuery) -> None:
    index = _callback_int(callback, 2)
    if index is None or not await _superadmin(callback):
        return
    async with async_session_factory() as session:
        service = SupportContactService(session)
        contacts = await service.definitions()
        if index >= len(contacts):
            await callback.answer("Контакт не найден.", show_alert=True)
            return
        if len(contacts) == 1:
            await callback.answer(
                "Нельзя удалить последний контакт. Сначала добавьте новый "
                "или верните настройки из .env.",
                show_alert=True,
            )
            return
        removed = contacts.pop(index)
        await service.save(contacts, callback.from_user.id)
        await AdminPermissionService(session).audit(
            callback.from_user.id,
            "support.contact.delete",
            "app_setting",
            metadata={"kind": removed.kind},
        )
        await session.commit()
    await support_settings(callback, _empty_state())


@router.callback_query(F.data == "cfg:supportresetask")
async def support_reset_ask(callback: CallbackQuery) -> None:
    if not await _superadmin(callback):
        return
    await callback.answer()
    await _edit(
        callback,
        "Удалить runtime-настройку и вернуть контакты из .env?",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Подтвердить",
                        callback_data="cfg:supportreset",
                    )
                ],
                [InlineKeyboardButton(text="Отмена", callback_data="cfg:support")],
            ]
        ),
    )


@router.callback_query(F.data == "cfg:supportreset")
async def support_reset(callback: CallbackQuery) -> None:
    if not await _superadmin(callback):
        return
    async with async_session_factory() as session:
        removed = await SupportContactService(session).reset_to_environment()
        await AdminPermissionService(session).audit(
            callback.from_user.id,
            "support.contacts.reset",
            "app_setting",
            metadata={"runtime_removed": removed},
        )
        await session.commit()
    await support_settings(callback, _empty_state())


@router.callback_query(F.data == "cfg:reminders")
async def reminder_settings(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    async with async_session_factory() as session:
        if not AdminPermissionService(session).is_superadmin(callback.from_user.id):
            await callback.answer("Только для superadmin.", show_alert=True)
            return
        settings = get_settings()
        intervals = await RuntimeSettingsService(
            session
        ).notification_intervals_minutes(
            settings.notification_intervals_minutes,
            settings.notification_days,
        )
    await callback.answer()
    await _edit(
        callback,
        "Напоминания об окончании\n\n"
        f"Интервалы: {', '.join(_format_interval(item) for item in intervals)}\n\n"
        "Интервалы больше или равные длительности тарифа автоматически пропускаются.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Изменить",
                        callback_data="cfg:remindersset",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Вернуть значения по умолчанию",
                        callback_data="cfg:remindersresetask",
                    )
                ],
                [InlineKeyboardButton(text="В меню", callback_data="admin:menu")],
            ]
        ),
    )


@router.callback_query(F.data == "cfg:remindersset")
async def reminder_set_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _superadmin(callback):
        return
    await state.set_state(ReminderAdminForm.intervals)
    await callback.answer()
    await _edit(
        callback,
        "Введите интервалы через запятую. Пример: 7d, 3d, 1d, 12h.",
        admin_cancel_keyboard("cfg:reminders"),
    )


@router.message(ReminderAdminForm.intervals)
async def reminder_set_save(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not await _superadmin_message(message):
        await state.clear()
        return
    try:
        intervals = _parse_intervals(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    async with async_session_factory() as session:
        await RuntimeSettingsService(session).set(
            RuntimeSettingsService.NOTIFICATION_INTERVALS_MINUTES,
            intervals,
            message.from_user.id,
        )
        await AdminPermissionService(session).audit(
            message.from_user.id,
            "settings.notification_intervals",
            "app_setting",
            metadata={"intervals_minutes": intervals},
        )
        await session.commit()
    await state.clear()
    await message.answer(
        "Интервалы сохранены: "
        + ", ".join(_format_interval(item) for item in intervals),
        reply_markup=admin_menu_keyboard(),
    )


@router.callback_query(F.data == "cfg:remindersresetask")
async def reminder_reset_ask(callback: CallbackQuery) -> None:
    if not await _superadmin(callback):
        return
    await callback.answer()
    await _edit(
        callback,
        "Удалить runtime-интервалы и вернуть значения из конфигурации?",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Подтвердить",
                        callback_data="cfg:remindersreset",
                    )
                ],
                [InlineKeyboardButton(text="Отмена", callback_data="cfg:reminders")],
            ]
        ),
    )


@router.callback_query(F.data == "cfg:remindersreset")
async def reminder_reset(callback: CallbackQuery) -> None:
    if not await _superadmin(callback):
        return
    async with async_session_factory() as session:
        runtime = RuntimeSettingsService(session)
        removed = await runtime.delete(
            RuntimeSettingsService.NOTIFICATION_INTERVALS_MINUTES
        )
        await AdminPermissionService(session).audit(
            callback.from_user.id,
            "settings.notification_intervals.reset",
            "app_setting",
            metadata={"runtime_removed": removed},
        )
        await session.commit()
    await reminder_settings(callback, _empty_state())


class _Denied:
    pass


_DENIED = _Denied()


async def _manage_scope(session, telegram_id: int) -> list[int] | None | _Denied:
    permissions = AdminPermissionService(session)
    if not await permissions.can_manage(telegram_id):
        return _DENIED
    return await permissions.channel_scope(telegram_id, require_manage=True)


async def _managed_plan(session, telegram_id: int, plan_id: int) -> Plan | None:
    plan = await session.get(
        Plan,
        plan_id,
        options=[selectinload(Plan.channel)],
    )
    if plan is None or not await AdminPermissionService(session).can_manage_channel(
        telegram_id,
        plan.channel_id,
    ):
        return None
    return plan


async def _managed_active_plans(session, telegram_id: int) -> list[Plan]:
    scope = await _manage_scope(session, telegram_id)
    if scope is _DENIED:
        return []
    statement = (
        select(Plan)
        .options(selectinload(Plan.channel))
        .where(Plan.is_active.is_(True))
        .order_by(Plan.channel_id, Plan.price)
    )
    if scope is not None:
        statement = (
            statement.where(Plan.channel_id.in_(scope))
            if scope
            else statement.where(False)
        )
    plans = list(await session.scalars(statement))
    settings = get_settings()
    materials_id = await RuntimeSettingsService(session).get(
        RuntimeSettingsService.MATERIALS_CHANNEL_ID,
        settings.materials_channel_id,
    )
    return [
        plan
        for plan in plans
        if plan.channel.is_active
        and (
            materials_id is None
            or plan.channel.telegram_chat_id != int(materials_id)
        )
    ]


async def _managed_promo(
    session,
    telegram_id: int,
    promo_id: int,
) -> PromoCode | None:
    promo = await session.get(
        PromoCode,
        promo_id,
        options=[selectinload(PromoCode.plan).selectinload(Plan.channel)],
    )
    if promo is None or not await AdminPermissionService(session).can_manage_channel(
        telegram_id,
        promo.plan.channel_id,
    ):
        return None
    return promo


async def _promo_locked_payment_count(session, promo_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count(Payment.id)).where(
                Payment.promo_code_id == promo_id,
                Payment.status.in_(
                    [PaymentStatus.PENDING, PaymentStatus.PAID]
                ),
            )
        )
        or 0
    )


async def _can_manage_message(message: Message) -> bool:
    if message.from_user is None:
        return False
    async with async_session_factory() as session:
        allowed = await AdminPermissionService(session).can_manage(message.from_user.id)
    if not allowed:
        await message.answer("Недостаточно прав.")
    return allowed


async def _superadmin(callback: CallbackQuery) -> bool:
    async with async_session_factory() as session:
        allowed = AdminPermissionService(session).is_superadmin(callback.from_user.id)
    if not allowed:
        await callback.answer("Только для superadmin.", show_alert=True)
    return allowed


async def _superadmin_message(message: Message) -> bool:
    if message.from_user is None:
        return False
    async with async_session_factory() as session:
        allowed = AdminPermissionService(session).is_superadmin(message.from_user.id)
    if not allowed:
        await message.answer("Только для superadmin.")
    return allowed


def _parse_utc_datetime(value: str, *, allow_now: bool = False) -> datetime:
    normalized = value.strip().lower()
    if allow_now and normalized in {"сейчас", "now"}:
        return datetime.now(UTC).replace(tzinfo=None, second=0, microsecond=0)
    try:
        return datetime.strptime(value.strip(), "%d.%m.%Y %H:%M")
    except ValueError as exc:
        hint = "`сейчас` или " if allow_now else ""
        raise ValueError(f"Формат: {hint}ДД.ММ.ГГГГ ЧЧ:ММ (UTC).") from exc


def _parse_intervals(value: str) -> list[int]:
    intervals: set[int] = set()
    for raw_item in value.replace(" ", "").lower().split(","):
        if not raw_item:
            continue
        suffix = raw_item[-1]
        if suffix not in {"d", "h", "m"}:
            raise ValueError("Используйте суффиксы d, h или m: 7d, 12h, 30m.")
        try:
            amount = int(raw_item[:-1])
        except ValueError as exc:
            raise ValueError("Интервалы должны быть целыми числами.") from exc
        factor = {"d": 1440, "h": 60, "m": 1}[suffix]
        minutes = amount * factor
        if minutes <= 0 or minutes > 525_600:
            raise ValueError("Каждый интервал должен быть от 1 минуты до 365 дней.")
        if minutes in intervals:
            raise ValueError("Интервалы не должны повторяться.")
        intervals.add(minutes)
    if not intervals:
        raise ValueError("Укажите хотя бы один интервал.")
    if len(intervals) > 12:
        raise ValueError("Можно настроить не более 12 интервалов.")
    return sorted(intervals, reverse=True)


def _format_interval(minutes: int) -> str:
    if minutes % 1440 == 0:
        return f"{minutes // 1440} дн."
    if minutes % 60 == 0:
        return f"{minutes // 60} ч."
    return f"{minutes} мин."


def _bounded_int(value: str | None, minimum: int, maximum: int) -> int:
    try:
        result = int((value or "").strip())
    except ValueError as exc:
        raise ValueError(f"Введите целое число от {minimum} до {maximum}.") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"Введите целое число от {minimum} до {maximum}.")
    return result


def _callback_int(callback: CallbackQuery, index: int) -> int | None:
    parts = (callback.data or "").split(":")
    if len(parts) <= index:
        return None
    try:
        return int(parts[index])
    except ValueError:
        return None


async def _edit(
    callback: CallbackQuery,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


class _EmptyState:
    async def clear(self) -> None:
        return None


def _empty_state() -> _EmptyState:
    return _EmptyState()
