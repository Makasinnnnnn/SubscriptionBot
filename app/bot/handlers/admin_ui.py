from decimal import Decimal

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.admin.pricing import parse_plan_price, validate_plan_price
from app.admin.service import AdminPermissionService
from app.bot.keyboards.admin import (
    admin_cancel_keyboard,
    admin_menu_keyboard,
    refund_confirmation_keyboard,
)
from app.db.enums import PaymentStatus, SubscriptionStatus
from app.db.models import (
    Channel,
    Payment,
    Plan,
    PromoCode,
    PromoRedemption,
    Subscription,
    User,
)
from app.db.session import async_session_factory
from app.payments.registry import get_payment_providers
from app.services.admin_notification_service import (
    NOTIFICATION_CATEGORIES,
    AdminNotificationService,
)
from app.subscriptions.presentation import access_delivery_text
from app.subscriptions.service import SubscriptionService

router = Router()

NOTIFICATION_LABELS = {
    "payments": "Платежи",
    "subscriptions": "Подписки",
    "refunds": "Возвраты",
    "errors": "Ошибки",
    "system": "Система",
}


class PlanForm(StatesGroup):
    title = State()
    duration = State()
    price = State()
    currency = State()
    edit_value = State()


class AccessForm(StatesGroup):
    user_id = State()
    grant_days = State()


@router.callback_query(F.data == "adm:plans")
async def plans_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not await _can_manage(callback):
        return
    await callback.answer()
    await _show_plans(callback, "active")


@router.callback_query(F.data.startswith("adm:plansfilter:"))
async def plans_filter(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not await _can_manage(callback):
        return
    filter_token = (callback.data or "").rsplit(":", 1)[-1]
    if filter_token not in {"active", "archive", "all"}:
        await callback.answer("Некорректный фильтр.", show_alert=True)
        return
    await callback.answer()
    await _show_plans(callback, filter_token)


@router.callback_query(F.data.startswith("adm:plan:"))
async def plan_card(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not await _can_manage(callback):
        return
    await callback.answer()
    plan_id = _callback_int(callback, 2)
    if plan_id is None:
        return
    await _show_plan(callback, plan_id)


@router.callback_query(F.data == "adm:planadd")
async def add_plan_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _can_manage(callback):
        return
    await callback.answer()
    async with async_session_factory() as session:
        channels = await _managed_channels(session, callback.from_user.id)
    if not channels:
        await _edit(callback, "Нет доступных активных каналов.", admin_menu_keyboard())
        return
    rows = [
        [InlineKeyboardButton(text=channel.title[:64], callback_data=f"adm:planaddch:{channel.id}")]
        for channel in channels
    ]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="adm:plans")])
    await state.clear()
    await _edit(
        callback,
        "Новый тариф\n\nВыберите канал:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("adm:planaddch:"))
async def add_plan_channel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _can_manage(callback):
        return
    channel_id = _callback_int(callback, 2)
    if channel_id is None:
        return
    async with async_session_factory() as session:
        if not await AdminPermissionService(session).can_manage_channel(
            callback.from_user.id, channel_id
        ):
            await callback.answer("Нет доступа к каналу.", show_alert=True)
            return
    await callback.answer()
    await state.set_state(PlanForm.title)
    await state.update_data(channel_id=channel_id)
    await _edit(
        callback, "Введите название тарифа, например: 30 дней", admin_cancel_keyboard("adm:plans")
    )


@router.message(PlanForm.title)
async def add_plan_title(message: Message, state: FSMContext) -> None:
    if not await _can_manage_message(message):
        await state.clear()
        return
    title = (message.text or "").strip()
    if not 2 <= len(title) <= 255:
        await message.answer("Название должно содержать от 2 до 255 символов.")
        return
    await state.update_data(title=title)
    await state.set_state(PlanForm.duration)
    await message.answer(
        "Введите срок действия в днях, например: 30",
        reply_markup=admin_cancel_keyboard("adm:plans"),
    )


@router.message(PlanForm.duration)
async def add_plan_duration(message: Message, state: FSMContext) -> None:
    if not await _can_manage_message(message):
        await state.clear()
        return
    try:
        duration = _positive_int(message.text, 1, 3650)
    except ValueError:
        await message.answer("Введите целое число от 1 до 3650.")
        return
    await state.update_data(duration_days=duration)
    await state.set_state(PlanForm.price)
    await message.answer("Введите цену, например: 990 или 12.50")


@router.message(PlanForm.price)
async def add_plan_price(message: Message, state: FSMContext) -> None:
    if not await _can_manage_message(message):
        await state.clear()
        return
    try:
        price = _price(message.text)
    except ValueError:
        await message.answer("Введите положительную цену не выше 9999999999.99.")
        return
    await state.update_data(price=str(price))
    await state.set_state(PlanForm.currency)
    await message.answer(
        "Выберите валюту:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="RUB", callback_data="adm:currency:RUB"),
                    InlineKeyboardButton(text="USDT", callback_data="adm:currency:USDT"),
                    InlineKeyboardButton(text="XTR", callback_data="adm:currency:XTR"),
                ],
                [InlineKeyboardButton(text="Отмена", callback_data="adm:plans")],
            ]
        ),
    )


@router.callback_query(PlanForm.currency, F.data.startswith("adm:currency:"))
async def add_plan_currency(callback: CallbackQuery, state: FSMContext) -> None:
    currency = (callback.data or "").rsplit(":", 1)[-1].upper()
    if currency not in {"RUB", "USDT", "XTR"}:
        await callback.answer("Неподдерживаемая валюта.", show_alert=True)
        return
    data = await state.get_data()
    price = Decimal(data["price"])
    try:
        price = validate_plan_price(price, currency)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    async with async_session_factory() as session:
        if not await AdminPermissionService(session).can_manage_channel(
            callback.from_user.id, data["channel_id"]
        ):
            await callback.answer("Нет доступа к каналу.", show_alert=True)
            return
        plan = Plan(
            channel_id=data["channel_id"],
            title=data["title"],
            duration_days=data["duration_days"],
            price=price,
            currency=currency,
            stars_price=int(price) if currency == "XTR" else None,
        )
        session.add(plan)
        await session.flush()
        await AdminPermissionService(session).audit(
            callback.from_user.id, "plan.create", "plan", plan.id
        )
        await session.commit()
        plan_id = plan.id
    await state.clear()
    await callback.answer("Тариф создан.")
    await _show_plan(callback, plan_id)


@router.callback_query(F.data.startswith("adm:planedit:"))
async def edit_plan_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _can_manage(callback):
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 4 or parts[3] not in {"title", "duration", "price", "currency", "stars_price"}:
        await callback.answer("Некорректное действие.", show_alert=True)
        return
    plan_id = _safe_int(parts[2])
    if plan_id is None:
        return
    async with async_session_factory() as session:
        plan = await session.get(Plan, plan_id)
        if plan is None or not await AdminPermissionService(session).can_manage_channel(
            callback.from_user.id, plan.channel_id
        ):
            await callback.answer("Тариф недоступен.", show_alert=True)
            return
    field = parts[3]
    labels = {
        "title": "новое название",
        "duration": "новый срок в днях",
        "price": "новую цену",
        "currency": "новую валюту (RUB, USDT или XTR)",
        "stars_price": "цену в Telegram Stars (целое число XTR)",
    }
    await state.set_state(PlanForm.edit_value)
    await state.update_data(plan_id=plan_id, field=field)
    await callback.answer()
    await _edit(callback, f"Введите {labels[field]}:", admin_cancel_keyboard(f"adm:plan:{plan_id}"))


@router.message(PlanForm.edit_value)
async def edit_plan_value(message: Message, state: FSMContext) -> None:
    if not await _can_manage_message(message):
        await state.clear()
        return
    data = await state.get_data()
    plan_id = int(data["plan_id"])
    field = data["field"]
    raw = (message.text or "").strip()
    try:
        value = _plan_field_value(field, raw)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    async with async_session_factory() as session:
        plan = await session.get(Plan, plan_id)
        if (
            plan is None
            or message.from_user is None
            or not await AdminPermissionService(session).can_manage_channel(
                message.from_user.id, plan.channel_id
            )
        ):
            await state.clear()
            await message.answer("Тариф недоступен.", reply_markup=admin_menu_keyboard())
            return
        model_field = "duration_days" if field == "duration" else field
        try:
            if field == "price":
                value = validate_plan_price(value, plan.currency)
            elif field == "currency":
                validate_plan_price(plan.price, value)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        old_value = str(getattr(plan, model_field))
        setattr(plan, model_field, value)
        if plan.currency == "XTR" and field in {"price", "currency"}:
            plan.stars_price = int(plan.price)
        await AdminPermissionService(session).audit(
            message.from_user.id,
            "plan.update",
            "plan",
            plan.id,
            {"field": model_field, "old": old_value, "new": str(value)},
        )
        await session.commit()
    await state.clear()
    await message.answer("Изменение сохранено.", reply_markup=_plan_return_keyboard(plan_id))


@router.callback_query(F.data.startswith("adm:plantoggle:"))
async def toggle_plan(callback: CallbackQuery) -> None:
    if not await _can_manage(callback):
        return
    plan_id = _callback_int(callback, 2)
    if plan_id is None:
        return
    async with async_session_factory() as session:
        plan = await session.get(Plan, plan_id)
        if plan is None or not await AdminPermissionService(session).can_manage_channel(
            callback.from_user.id, plan.channel_id
        ):
            await callback.answer("Тариф недоступен.", show_alert=True)
            return
        plan.is_active = not plan.is_active
        await AdminPermissionService(session).audit(
            callback.from_user.id,
            "plan.enable" if plan.is_active else "plan.disable",
            "plan",
            plan.id,
        )
        await session.commit()
    await callback.answer("Статус изменён.")
    await _show_plan(callback, plan_id)


@router.callback_query(F.data == "adm:access")
async def access_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not await _can_manage(callback):
        return
    await callback.answer()
    await _show_access_users(callback, 0)


@router.callback_query(F.data.startswith("adm:accesspage:"))
async def access_users_page(callback: CallbackQuery) -> None:
    if not await _can_manage(callback):
        return
    page = _callback_int(callback, 2)
    if page is None:
        return
    await callback.answer()
    await _show_access_users(callback, page)


@router.callback_query(F.data.startswith("adm:accessuser:"))
async def access_user_card(callback: CallbackQuery) -> None:
    if not await _can_manage(callback):
        return
    telegram_id = _callback_int(callback, 2)
    if telegram_id is None:
        return
    await callback.answer()
    await _show_user_access(callback, telegram_id)


@router.callback_query(F.data == "adm:stars")
async def stars_dashboard(callback: CallbackQuery) -> None:
    if not await _can_manage(callback):
        return
    await callback.answer()
    async with async_session_factory() as session:
        global_access = await AdminPermissionService(session).can_manage_all_channels(
            callback.from_user.id
        )
    balance = None
    telegram_transactions = None
    if global_access:
        try:
            balance, telegram_transactions = await _telegram_stars_data(callback)
        except TelegramAPIError:
            await _edit(
                callback,
                "Telegram Stars\n\nНе удалось получить баланс из Telegram. Попробуйте ещё раз позже.",
                _stars_back_keyboard(),
            )
            return

    async with async_session_factory() as session:
        channels = await _managed_channels(session, callback.from_user.id)
        channel_ids = [channel.id for channel in channels]
        received = Decimal(0)
        refunded = Decimal(0)
        payments: list[Payment] = []
        if channel_ids:
            received = await session.scalar(
                select(func.coalesce(func.sum(Payment.amount), 0))
                .join(Payment.plan)
                .where(
                    func.coalesce(Payment.purchased_channel_id, Plan.channel_id).in_(channel_ids),
                    Payment.provider == "telegram-stars",
                    Payment.status.in_([PaymentStatus.PAID, PaymentStatus.REFUNDED]),
                )
            )
            refunded = await session.scalar(
                select(func.coalesce(func.sum(Payment.amount), 0))
                .join(Payment.plan)
                .where(
                    func.coalesce(Payment.purchased_channel_id, Plan.channel_id).in_(channel_ids),
                    Payment.provider == "telegram-stars",
                    Payment.status == PaymentStatus.REFUNDED,
                )
            )
            payments = list(
                await session.scalars(
                    select(Payment)
                    .join(Payment.plan)
                    .options(selectinload(Payment.user), selectinload(Payment.plan))
                    .where(
                        func.coalesce(Payment.purchased_channel_id, Plan.channel_id).in_(
                            channel_ids
                        ),
                        Payment.provider == "telegram-stars",
                    )
                    .order_by(Payment.created_at.desc())
                    .limit(10)
                )
            )

    lines = [
        "Telegram Stars",
        "",
        f"Получено по платежам: {_star_amount(received, None)} XTR",
        f"Возвращено: {_star_amount(refunded, None)} XTR",
        f"Чистая сумма в базе: {_star_amount(received - refunded, None)} XTR",
        "",
    ]
    if balance is not None and telegram_transactions is not None:
        lines.insert(2, f"Баланс бота: {_star_amount(balance.amount, balance.nanostar_amount)} XTR")
        lines.append("Последние операции Telegram:")
        if telegram_transactions.transactions:
            lines.extend(
                f"{transaction.date:%d.%m %H:%M} | {transaction.amount:+d} XTR | {transaction.id[:12]}"
                for transaction in telegram_transactions.transactions[:8]
            )
        else:
            lines.append("Операций пока нет.")

    rows = []
    refundable = [payment for payment in payments if payment.status == PaymentStatus.PAID]
    if refundable:
        rows.append([InlineKeyboardButton(text="Возвраты", callback_data="adm:starrefunds")])
    rows.extend(
        [
            [InlineKeyboardButton(text="Обновить", callback_data="adm:stars")],
            [InlineKeyboardButton(text="Назад", callback_data="admin:menu")],
        ]
    )
    await _edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "adm:notifications")
async def notification_settings(callback: CallbackQuery) -> None:
    if not await _can_manage(callback):
        return
    await callback.answer()
    async with async_session_factory() as session:
        values = await AdminNotificationService(session, callback.bot).preferences(
            callback.from_user.id
        )
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'Включено' if values[category] else 'Выключено'} | {NOTIFICATION_LABELS[category]}",
                callback_data=f"adm:notiftoggle:{category}",
            )
        ]
        for category in NOTIFICATION_CATEGORIES
    ]
    rows.extend(
        [
            [
                InlineKeyboardButton(text="Включить всё", callback_data="adm:notifall:on"),
                InlineKeyboardButton(text="Отключить всё", callback_data="adm:notifall:off"),
            ],
            [InlineKeyboardButton(text="Назад", callback_data="admin:menu")],
        ]
    )
    await _edit(
        callback,
        "Уведомления\n\nВыберите категории событий, которые хотите получать:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("adm:notiftoggle:"))
async def notification_toggle(callback: CallbackQuery) -> None:
    if not await _can_manage(callback):
        return
    category = (callback.data or "").rsplit(":", 1)[-1]
    if category not in NOTIFICATION_CATEGORIES:
        await callback.answer("Неизвестная категория.", show_alert=True)
        return
    async with async_session_factory() as session:
        service = AdminNotificationService(session, callback.bot)
        current = (await service.preferences(callback.from_user.id))[category]
        await service.set_preference(callback.from_user.id, category, not current)
        await AdminPermissionService(session).audit(
            callback.from_user.id,
            "admin_notifications.update",
            "admin",
            callback.from_user.id,
            {"category": category, "enabled": not current},
        )
        await session.commit()
    await notification_settings(callback)


@router.callback_query(F.data.startswith("adm:notifall:"))
async def notification_toggle_all(callback: CallbackQuery) -> None:
    if not await _can_manage(callback):
        return
    enabled = (callback.data or "").endswith(":on")
    async with async_session_factory() as session:
        service = AdminNotificationService(session, callback.bot)
        await service.set_all_preferences(callback.from_user.id, enabled)
        await AdminPermissionService(session).audit(
            callback.from_user.id,
            "admin_notifications.update_all",
            "admin",
            callback.from_user.id,
            {"enabled": enabled},
        )
        await session.commit()
    await notification_settings(callback)


@router.callback_query(F.data == "adm:starrefunds")
async def stars_refunds(callback: CallbackQuery) -> None:
    if not await _can_manage(callback):
        return
    await callback.answer()
    async with async_session_factory() as session:
        channels = await _managed_channels(session, callback.from_user.id)
        channel_ids = [channel.id for channel in channels]
        payments = (
            list(
                await session.scalars(
                    select(Payment)
                    .join(Payment.plan)
                    .options(selectinload(Payment.user), selectinload(Payment.plan))
                    .where(
                        func.coalesce(Payment.purchased_channel_id, Plan.channel_id).in_(
                            channel_ids
                        ),
                        Payment.provider == "telegram-stars",
                        Payment.status == PaymentStatus.PAID,
                    )
                    .order_by(Payment.paid_at.desc())
                    .limit(20)
                )
            )
            if channel_ids
            else []
        )
    rows = [
        [
            InlineKeyboardButton(
                text=f"#{payment.id} | {payment.amount:g} XTR | {payment.user.telegram_id}",
                callback_data=f"adm:starrefund:{payment.id}",
            )
        ]
        for payment in payments
    ]
    rows.append([InlineKeyboardButton(text="Назад", callback_data="adm:stars")])
    await _edit(
        callback,
        "Возврат Stars\n\nВыберите оплаченный заказ. После подтверждения будет убран неиспользованный срок этой оплаты:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("adm:starrefund:"))
async def stars_refund_request(callback: CallbackQuery) -> None:
    if not await _can_manage(callback):
        return
    payment_id = _callback_int(callback, 2)
    if payment_id is None:
        return
    async with async_session_factory() as session:
        payment = await session.get(Payment, payment_id, options=[selectinload(Payment.plan)])
        allowed = (
            payment is not None
            and payment.provider == "telegram-stars"
            and payment.status == PaymentStatus.PAID
            and await AdminPermissionService(session).can_manage_channel(
                callback.from_user.id, payment.access_channel_id
            )
        )
    if not allowed or payment is None:
        await callback.answer("Платёж уже изменён или недоступен.", show_alert=True)
        return
    await callback.answer()
    await _edit(
        callback,
        f"Вернуть {payment.amount:g} XTR по платежу #{payment.id}?\n\nБудет убран неиспользованный срок этой оплаты. Остальные периоды сохранятся.",
        refund_confirmation_keyboard(payment.id),
    )


@router.callback_query(F.data.in_({"adm:userfind", "adm:grant"}))
async def access_enter_user(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _can_manage(callback):
        return
    await state.set_state(AccessForm.user_id)
    await state.update_data(action="grant" if callback.data == "adm:grant" else "find")
    await callback.answer()
    await _edit(
        callback,
        "Введите Telegram ID или @username пользователя:",
        admin_cancel_keyboard("adm:access"),
    )


@router.message(AccessForm.user_id)
async def access_user_received(message: Message, state: FSMContext) -> None:
    if not await _can_manage_message(message):
        await state.clear()
        return
    async with async_session_factory() as session:
        telegram_id = await _resolve_user_reference(session, message.text)
    if telegram_id is None:
        await message.answer(
            "Пользователь не найден. Он должен хотя бы один раз открыть бота и нажать /start. "
            "Также можно указать его числовой Telegram ID."
        )
        return
    data = await state.get_data()
    await state.clear()
    if data.get("action") == "grant":
        if message.from_user is None:
            return
        await _show_grant_channels(message, telegram_id, message.from_user.id)
    else:
        await _show_user_access(message, telegram_id)


@router.callback_query(F.data.startswith("adm:grantch:"))
async def grant_channel(callback: CallbackQuery, state: FSMContext) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        return
    telegram_id, channel_id = _safe_int(parts[2]), _safe_int(parts[3])
    if telegram_id is None or channel_id is None:
        return
    async with async_session_factory() as session:
        if not await AdminPermissionService(session).can_manage_channel(
            callback.from_user.id, channel_id
        ):
            await callback.answer("Нет доступа к каналу.", show_alert=True)
            return
    await state.set_state(AccessForm.grant_days)
    await state.update_data(telegram_id=telegram_id, channel_id=channel_id)
    await callback.answer()
    await _edit(callback, "На сколько дней выдать доступ?", admin_cancel_keyboard("adm:access"))


@router.message(AccessForm.grant_days)
async def grant_days_received(message: Message, state: FSMContext) -> None:
    if not await _can_manage_message(message):
        await state.clear()
        return
    try:
        days = _positive_int(message.text, 1, 3650)
    except ValueError:
        await message.answer("Введите целое число от 1 до 3650.")
        return
    data = await state.get_data()
    async with async_session_factory() as session:
        if message.from_user is None or not await AdminPermissionService(
            session
        ).can_manage_channel(message.from_user.id, data["channel_id"]):
            await message.answer("Нет доступа к каналу.")
            return
        try:
            subscription, invite = await SubscriptionService(session).grant_manual(
                data["telegram_id"], data["channel_id"], days, message.bot
            )
        except (ValueError, TelegramAPIError):
            await session.rollback()
            await message.answer(
                "Не удалось выдать доступ. Проверьте настройки канала и права бота, затем повторите попытку."
            )
            return
        await AdminPermissionService(session).audit(
            message.from_user.id,
            "subscription.grant",
            "subscription",
            subscription.id,
            {"telegram_id": data["telegram_id"], "days": days},
        )
        await session.commit()
    await state.clear()
    if invite.materials_link:
        try:
            await message.bot.send_message(
                data["telegram_id"],
                access_delivery_text(
                    expires_at=subscription.expires_at,
                    primary_link=invite.link,
                    materials_link=invite.materials_link,
                ),
            )
        except TelegramAPIError:
            await message.answer(
                "Не удалось отправить ссылки пользователю.", reply_markup=admin_menu_keyboard()
            )
        else:
            await message.answer(
                "Доступ к обоим каналам выдан и отправлен одним сообщением.",
                reply_markup=admin_menu_keyboard(),
            )
        return
    try:
        await message.bot.send_message(
            data["telegram_id"],
            f"Вам выдан доступ до {subscription.expires_at:%d.%m.%Y %H:%M}.\n{invite.link}",
        )
        delivery = "Ссылка отправлена пользователю."
    except TelegramAPIError:
        delivery = f"Передайте ссылку вручную: {invite.link}"
    await message.answer(
        f"Доступ выдан до {subscription.expires_at:%d.%m.%Y %H:%M}.\n{delivery}",
        reply_markup=admin_menu_keyboard(),
    )


@router.callback_query(F.data.startswith("adm:revoke:"))
async def revoke_subscription_confirm(callback: CallbackQuery) -> None:
    if not await _can_manage(callback):
        return
    subscription_id = _callback_int(callback, 2)
    if subscription_id is None:
        return
    await callback.answer()
    await _edit(
        callback,
        "Отозвать доступ? Пользователь будет удалён из канала, а подписка отменена.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Да, отозвать",
                        callback_data=f"adm:revokeok:{subscription_id}",
                    )
                ],
                [InlineKeyboardButton(text="Отмена", callback_data="adm:access")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("adm:revokeok:"))
async def revoke_subscription(callback: CallbackQuery) -> None:
    if not await _can_manage(callback):
        return
    subscription_id = _callback_int(callback, 2)
    if subscription_id is None:
        return
    async with async_session_factory() as session:
        subscription = await session.get(
            Subscription,
            subscription_id,
            options=[selectinload(Subscription.user)],
        )
        if subscription is None or not await AdminPermissionService(session).can_manage_channel(
            callback.from_user.id, subscription.channel_id
        ):
            await callback.answer("Подписка недоступна.", show_alert=True)
            return
        result = await SubscriptionService(session).revoke(
            subscription_id, callback.bot, "revoked from admin menu"
        )
        await AdminPermissionService(session).audit(
            callback.from_user.id,
            "subscription.revoke",
            "subscription",
            subscription_id,
            {"telegram_changed": result.telegram_access_changed, "error": result.error},
        )
        await session.commit()
        telegram_id = subscription.user.telegram_id
    await callback.answer("Доступ отозван.")
    await _show_user_access(callback, telegram_id)


async def _show_plans(callback: CallbackQuery, filter_token: str = "active") -> None:
    async with async_session_factory() as session:
        channels = await _managed_channels(session, callback.from_user.id)
        channel_ids = [channel.id for channel in channels]
        statement = (
            select(Plan)
            .options(selectinload(Plan.channel))
            .where(Plan.channel_id.in_(channel_ids))
            .order_by(Plan.channel_id, Plan.is_active.desc(), Plan.price)
        )
        if filter_token == "active":
            statement = statement.where(Plan.is_active.is_(True))
        elif filter_token == "archive":
            statement = statement.where(Plan.is_active.is_(False))
        plans = list(await session.scalars(statement)) if channel_ids else []
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'Активен' if plan.is_active else 'Архив'} | {plan.title} | {plan.price:g} {plan.currency}",
                callback_data=f"adm:plan:{plan.id}",
            )
        ]
        for plan in plans
    ]
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text=f"{'✓ ' if filter_token == 'active' else ''}В продаже",
                    callback_data="adm:plansfilter:active",
                ),
                InlineKeyboardButton(
                    text=f"{'✓ ' if filter_token == 'archive' else ''}Архив",
                    callback_data="adm:plansfilter:archive",
                ),
                InlineKeyboardButton(
                    text=f"{'✓ ' if filter_token == 'all' else ''}Все",
                    callback_data="adm:plansfilter:all",
                ),
            ],
            [InlineKeyboardButton(text="Добавить тариф", callback_data="adm:planadd")],
            [InlineKeyboardButton(text="Назад", callback_data="admin:menu")],
        ]
    )
    filter_label = {
        "active": "в продаже",
        "archive": "архив",
        "all": "все",
    }[filter_token]
    await _edit(
        callback,
        f"Тарифы: {filter_label}\n\nНайдено: {len(plans)}",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _show_plan(callback: CallbackQuery, plan_id: int) -> None:
    async with async_session_factory() as session:
        plan = await session.get(Plan, plan_id, options=[selectinload(Plan.channel)])
        allowed = plan is not None and await AdminPermissionService(session).can_manage_channel(
            callback.from_user.id, plan.channel_id
        )
        if plan is not None and allowed:
            now = func.now()
            active_subscriptions = int(
                await session.scalar(
                    select(func.count(Subscription.id)).where(
                        Subscription.channel_id == plan.channel_id,
                        Subscription.status == SubscriptionStatus.ACTIVE,
                        Subscription.expires_at > now,
                        or_(
                            exists(
                                select(Payment.id).where(
                                    Payment.id == Subscription.payment_id,
                                    Payment.plan_id == plan.id,
                                )
                            ),
                            exists(
                                select(PromoRedemption.id)
                                .join(PromoCode)
                                .where(
                                    PromoRedemption.subscription_id == Subscription.id,
                                    PromoCode.plan_id == plan.id,
                                )
                            ),
                        ),
                    )
                )
                or 0
            )
            successful_payments = int(
                await session.scalar(
                    select(func.count(Payment.id)).where(
                        Payment.plan_id == plan.id,
                        Payment.status.in_([PaymentStatus.PAID, PaymentStatus.REFUNDED]),
                    )
                )
                or 0
            )
            last_purchase = await session.scalar(
                select(func.max(Payment.paid_at)).where(
                    Payment.plan_id == plan.id,
                    Payment.status.in_([PaymentStatus.PAID, PaymentStatus.REFUNDED]),
                )
            )
            revenue_rows = list(
                (
                    await session.execute(
                        select(
                            Payment.currency,
                            func.sum(
                                case(
                                    (Payment.status == PaymentStatus.PAID, Payment.amount),
                                    else_=0,
                                )
                            ),
                        )
                        .where(
                            Payment.plan_id == plan.id,
                            Payment.status.in_([PaymentStatus.PAID, PaymentStatus.REFUNDED]),
                        )
                        .group_by(Payment.currency)
                        .order_by(Payment.currency)
                    )
                ).all()
            )
    if not allowed or plan is None:
        await callback.answer("Тариф недоступен.", show_alert=True)
        return
    provider_names = set(get_payment_providers())
    available_methods = []
    if "telegram-stars" in provider_names and plan.stars_price:
        available_methods.append("Stars")
    if "cryptobot" in provider_names:
        available_methods.append("Crypto Pay")
    if "yookassa" in provider_names:
        available_methods.append("ЮKassa")
    if "dev" in provider_names:
        available_methods.append("Dev")
    revenue = ", ".join(f"{amount:g} {currency}" for currency, amount in revenue_rows) or "нет"
    warning = (
        "\nПредупреждение: Stars включены, но цена в Stars не задана."
        if "telegram-stars" in provider_names and not plan.stars_price
        else ""
    )
    rows = [
        [
            InlineKeyboardButton(text="Название", callback_data=f"adm:planedit:{plan.id}:title"),
            InlineKeyboardButton(text="Срок", callback_data=f"adm:planedit:{plan.id}:duration"),
        ],
        [
            InlineKeyboardButton(text="Цена", callback_data=f"adm:planedit:{plan.id}:price"),
            InlineKeyboardButton(text="Валюта", callback_data=f"adm:planedit:{plan.id}:currency"),
        ],
        [
            InlineKeyboardButton(
                text="Цена в Stars", callback_data=f"adm:planedit:{plan.id}:stars_price"
            )
        ],
        [
            InlineKeyboardButton(
                text="Архивировать" if plan.is_active else "Вернуть в продажу",
                callback_data=f"adm:plantoggle:{plan.id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="К списку",
                callback_data=(
                    "adm:plansfilter:active" if plan.is_active else "adm:plansfilter:archive"
                ),
            )
        ],
    ]
    await _edit(
        callback,
        f"{plan.channel.title}\n\n"
        f"Тариф: {plan.title}\n"
        f"Срок: {plan.duration_days} дн.\n"
        f"Цена: {plan.price:g} {plan.currency}\n"
        f"Цена Stars: {plan.stars_price or 'не задана'} XTR\n"
        f"Статус: {'в продаже' if plan.is_active else 'в архиве'}\n\n"
        f"Активных подписок тарифа: {active_subscriptions}\n"
        f"Успешных платежей тарифа: {successful_payments}\n"
        f"Последняя покупка: {_format_optional_datetime(last_purchase)}\n"
        f"Получено: {revenue}\n"
        f"Оплата: {', '.join(available_methods) or 'нет доступных способов'}"
        f"{warning}",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _show_grant_channels(
    message: Message,
    telegram_id: int,
    actor_telegram_id: int,
) -> None:
    async with async_session_factory() as session:
        channels = await _managed_channels(session, actor_telegram_id)
    if not channels:
        await message.answer(
            "Нет доступных активных каналов. Проверьте статус канала и права администратора.",
            reply_markup=admin_menu_keyboard(),
        )
        return
    rows = [
        [
            InlineKeyboardButton(
                text=channel.title[:64], callback_data=f"adm:grantch:{telegram_id}:{channel.id}"
            )
        ]
        for channel in channels
    ]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="adm:access")])
    await message.answer(
        f"Выберите канал для пользователя {telegram_id}:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _show_access_users(callback: CallbackQuery, page: int) -> None:
    page_size = 10
    async with async_session_factory() as session:
        scope = await AdminPermissionService(session).channel_scope(
            callback.from_user.id,
            require_manage=True,
        )
        users_query = select(User).order_by(User.id.desc())
        count_query = select(func.count(User.id))
        if scope is not None:
            visible_users = select(Subscription.user_id).where(Subscription.channel_id.in_(scope))
            users_query = users_query.where(User.id.in_(visible_users))
            count_query = count_query.where(User.id.in_(visible_users))
        total = await session.scalar(count_query) or 0
        max_page = max(0, (total - 1) // page_size)
        page = min(max(page, 0), max_page)
        users = list(await session.scalars(users_query.offset(page * page_size).limit(page_size)))

    rows: list[list[InlineKeyboardButton]] = []
    for user in users:
        if user.username:
            identity = f"@{user.username}"
        else:
            name = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
            identity = name or f"ID {user.telegram_id}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{identity} | {user.telegram_id}"[:64],
                    callback_data=f"adm:accessuser:{user.telegram_id}",
                )
            ]
        )

    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(text="Назад", callback_data=f"adm:accesspage:{page - 1}")
        )
    if page < max_page:
        navigation.append(
            InlineKeyboardButton(text="Далее", callback_data=f"adm:accesspage:{page + 1}")
        )
    if navigation:
        rows.append(navigation)
    rows.extend(
        [
            [InlineKeyboardButton(text="Найти пользователя", callback_data="adm:userfind")],
            [InlineKeyboardButton(text="Выдать доступ", callback_data="adm:grant")],
            [InlineKeyboardButton(text="В меню", callback_data="admin:menu")],
        ]
    )
    await _edit(
        callback,
        f"Управление доступом\n\nПользователи: {total}\nСтраница {page + 1} из {max_page + 1}",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _show_user_access(target: Message | CallbackQuery, telegram_id: int) -> None:
    actor_id = target.from_user.id
    async with async_session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        subscriptions = (
            []
            if user is None
            else list(
                await session.scalars(
                    select(Subscription)
                    .options(selectinload(Subscription.channel))
                    .where(
                        Subscription.user_id == user.id,
                        Subscription.status == SubscriptionStatus.ACTIVE,
                        Subscription.expires_at > func.now(),
                    )
                    .order_by(Subscription.expires_at.desc())
                )
            )
        )
        permissions = AdminPermissionService(session)
        subscriptions = [
            item
            for item in subscriptions
            if await permissions.can_manage_channel(actor_id, item.channel_id)
        ]
    lines = [f"Пользователь: {telegram_id}"]
    rows = []
    if user and user.username:
        lines.append(f"Username: @{user.username}")
    if subscriptions:
        lines.append("\nАктивные подписки:")
        for item in subscriptions:
            lines.append(f"{item.channel.title}: до {item.expires_at:%d.%m.%Y %H:%M}")
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"Отозвать: {item.channel.title}"[:64],
                        callback_data=f"adm:revoke:{item.id}",
                    )
                ]
            )
    else:
        lines.append("\nАктивных подписок нет.")
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="Выдать доступ", callback_data=f"adm:grantuser:{telegram_id}"
                )
            ],
            [InlineKeyboardButton(text="Назад", callback_data="adm:access")],
        ]
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    if isinstance(target, CallbackQuery):
        await _edit(target, "\n".join(lines), keyboard)
    else:
        await target.answer("\n".join(lines), reply_markup=keyboard)


@router.callback_query(F.data.startswith("adm:grantuser:"))
async def grant_known_user(callback: CallbackQuery) -> None:
    if not await _can_manage(callback):
        return
    telegram_id = _callback_int(callback, 2)
    if telegram_id is None or callback.message is None:
        return
    await callback.answer()
    await _show_grant_channels(callback.message, telegram_id, callback.from_user.id)


async def _managed_channels(session, telegram_id: int) -> list[Channel]:
    channels = list(
        await session.scalars(
            select(Channel).where(Channel.is_active.is_(True)).order_by(Channel.title)
        )
    )
    permissions = AdminPermissionService(session)
    return [
        channel
        for channel in channels
        if await permissions.can_manage_channel(telegram_id, channel.id)
    ]


async def _resolve_user_reference(
    session: AsyncSession,
    raw: str | None,
) -> int | None:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        return _positive_int(value, 1, 10**15)
    except ValueError:
        pass
    username = value.removeprefix("@").strip().lower()
    if not username:
        return None
    return await session.scalar(
        select(User.telegram_id)
        .where(func.lower(User.username) == username)
        .order_by(User.updated_at.desc())
        .limit(1)
    )


async def _can_manage(callback: CallbackQuery) -> bool:
    async with async_session_factory() as session:
        allowed = await AdminPermissionService(session).can_manage(callback.from_user.id)
    if not allowed:
        await callback.answer("Недостаточно прав.", show_alert=True)
    return allowed


async def _can_manage_message(message: Message) -> bool:
    if message.from_user is None:
        return False
    async with async_session_factory() as session:
        allowed = await AdminPermissionService(session).can_manage(message.from_user.id)
    if not allowed:
        await message.answer("Недостаточно прав.")
    return allowed


async def _telegram_stars_data(callback: CallbackQuery):
    balance = await callback.bot.get_my_star_balance()
    transactions = await callback.bot.get_star_transactions(limit=20)
    return balance, transactions


def _star_amount(amount: int | Decimal, nanostar_amount: int | None) -> str:
    if not nanostar_amount:
        return format(Decimal(amount).normalize(), "f")
    fraction = f"{nanostar_amount:09d}".rstrip("0")
    return f"{amount}.{fraction}"


def _format_optional_datetime(value) -> str:
    return value.strftime("%d.%m.%Y %H:%M") if value else "—"


def _stars_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Повторить", callback_data="adm:stars")],
            [InlineKeyboardButton(text="Назад", callback_data="admin:menu")],
        ]
    )


async def _edit(callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup) -> None:
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise


def _plan_return_keyboard(plan_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Вернуться к тарифу", callback_data=f"adm:plan:{plan_id}")]
        ]
    )


def _callback_int(callback: CallbackQuery, index: int) -> int | None:
    parts = (callback.data or "").split(":")
    return _safe_int(parts[index]) if len(parts) > index else None


def _safe_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _positive_int(raw: str | None, minimum: int, maximum: int) -> int:
    value = int((raw or "").strip())
    if not minimum <= value <= maximum:
        raise ValueError
    return value


def _price(raw: str | None) -> Decimal:
    return parse_plan_price(raw)


def _plan_field_value(field: str, raw: str):
    if field == "title":
        if not 2 <= len(raw) <= 255:
            raise ValueError("Название должно содержать от 2 до 255 символов.")
        return raw
    if field == "duration":
        try:
            return _positive_int(raw, 1, 3650)
        except ValueError as exc:
            raise ValueError("Введите целое число от 1 до 3650.") from exc
    if field == "price":
        try:
            return _price(raw)
        except ValueError as exc:
            raise ValueError("Введите положительную цену не выше 9999999999.99.") from exc
    if field == "stars_price":
        try:
            return _positive_int(raw, 1, 10_000_000)
        except ValueError as exc:
            raise ValueError("Введите целое количество Stars от 1 до 10000000.") from exc
    currency = raw.upper()
    if currency not in {"RUB", "USDT", "XTR"}:
        raise ValueError("Поддерживаются RUB, USDT и XTR.")
    return currency
