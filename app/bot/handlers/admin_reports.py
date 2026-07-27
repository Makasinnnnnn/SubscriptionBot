from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import and_, exists, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.admin.service import AdminPermissionService
from app.bot.keyboards.admin import (
    admin_cancel_keyboard,
    admin_menu_keyboard,
    refund_confirmation_keyboard,
)
from app.db.enums import PaymentStatus, SubscriptionStatus
from app.db.models import Channel, Payment, Plan, Subscription, User
from app.db.session import async_session_factory
from app.subscriptions.presentation import access_delivery_text
from app.subscriptions.service import SubscriptionService
from app.services.xlsx_export_service import MAX_EXPORT_ROWS, build_xlsx

router = Router()
PAGE_SIZE = 10

PAYMENT_PROVIDER_TOKENS = {
    "all": None,
    "stars": "telegram-stars",
    "crypto": "cryptobot",
    "yoo": "yookassa",
}
PAYMENT_STATUS_TOKENS = {
    "all",
    "pending",
    "paid",
    "failed",
    "expired",
    "refunded",
    "attention",
}
SUBSCRIPTION_FILTERS = {"all", "active", "expiring", "expired", "cancelled"}


class ReportActionForm(StatesGroup):
    extend_days = State()


@router.callback_query(F.data.startswith("rpt:users:"))
async def users_report(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        return
    page = _safe_nonnegative_int(parts[3])
    if page is None:
        return
    async with async_session_factory() as session:
        scope = await _authorized_scope(session, callback.from_user.id)
        if scope is _DENIED:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        statement = _users_statement(parts[2], scope)
        total = await _statement_count(session, statement)
        max_page = max(0, (total - 1) // PAGE_SIZE)
        page = min(page, max_page)
        users = list(
            await session.scalars(
                statement.order_by(User.id.desc())
                .offset(page * PAGE_SIZE)
                .limit(PAGE_SIZE)
            )
        )
    await callback.answer()
    rows = [
        [
            InlineKeyboardButton(
                text=f"{_user_identity(user)} | {user.telegram_id}"[:64],
                callback_data=f"adm:accessuser:{user.telegram_id}",
            )
        ]
        for user in users
    ]
    rows.extend(_user_filter_rows(parts[2]))
    navigation = _navigation(
        page,
        max_page,
        lambda target: f"rpt:users:{parts[2]}:{target}",
    )
    if navigation:
        rows.append(navigation)
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="Экспорт XLSX",
                    callback_data=f"rpt:export:users:{parts[2]}",
                )
            ],
            [InlineKeyboardButton(text="По каналу", callback_data="rpt:userchannels")],
            [InlineKeyboardButton(text="Найти", callback_data="adm:userfind")],
            [InlineKeyboardButton(text="Выдать доступ", callback_data="adm:grant")],
            [InlineKeyboardButton(text="В меню", callback_data="admin:menu")],
        ]
    )
    await _edit(
        callback,
        f"Доступы и пользователи\n\nФильтр: {_user_filter_label(parts[2])}\n"
        f"Найдено: {total}\nСтраница {page + 1} из {max_page + 1}",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "rpt:userchannels")
async def users_channel_filter(callback: CallbackQuery) -> None:
    async with async_session_factory() as session:
        scope = await _authorized_scope(session, callback.from_user.id)
        if scope is _DENIED:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        statement = select(Channel).order_by(Channel.title)
        if scope is not None:
            statement = statement.where(Channel.id.in_(scope)) if scope else statement.where(false())
        channels = list(await session.scalars(statement))
    await callback.answer()
    rows = [
        [
            InlineKeyboardButton(
                text=channel.title[:64],
                callback_data=f"rpt:users:c{channel.id}:0",
            )
        ]
        for channel in channels
    ]
    rows.append([InlineKeyboardButton(text="Назад", callback_data="rpt:users:all:0")])
    await _edit(
        callback,
        "Фильтр пользователей по каналу:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("rpt:subs:"))
async def subscriptions_report(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 4 or parts[2] not in SUBSCRIPTION_FILTERS:
        return
    page = _safe_nonnegative_int(parts[3])
    if page is None:
        return
    async with async_session_factory() as session:
        scope = await _authorized_scope(session, callback.from_user.id)
        if scope is _DENIED:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        statement = _subscriptions_statement(parts[2], scope)
        total = await _statement_count(session, statement)
        max_page = max(0, (total - 1) // PAGE_SIZE)
        page = min(page, max_page)
        subscriptions = list(
            await session.scalars(
                statement.options(
                    selectinload(Subscription.user),
                    selectinload(Subscription.channel),
                )
                .order_by(Subscription.id.desc())
                .offset(page * PAGE_SIZE)
                .limit(PAGE_SIZE)
            )
        )
    await callback.answer()
    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"{_user_identity(item.user)} | {item.channel.title} | "
                    f"{item.status.value}"
                )[:64],
                callback_data=f"rpt:sub:{item.id}",
            )
        ]
        for item in subscriptions
    ]
    rows.extend(_subscription_filter_rows(parts[2]))
    navigation = _navigation(
        page,
        max_page,
        lambda target: f"rpt:subs:{parts[2]}:{target}",
    )
    if navigation:
        rows.append(navigation)
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="Экспорт XLSX",
                    callback_data=f"rpt:export:subs:{parts[2]}",
                )
            ],
            [InlineKeyboardButton(text="В меню", callback_data="admin:menu")],
        ]
    )
    await _edit(
        callback,
        f"Подписки\n\nФильтр: {_subscription_filter_label(parts[2])}\n"
        f"Найдено: {total}\nСтраница {page + 1} из {max_page + 1}",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("rpt:sub:"))
async def subscription_card(callback: CallbackQuery) -> None:
    subscription_id = _callback_int(callback, 2)
    if subscription_id is None:
        return
    async with async_session_factory() as session:
        subscription = await session.get(
            Subscription,
            subscription_id,
            options=[
                selectinload(Subscription.user),
                selectinload(Subscription.channel),
                selectinload(Subscription.payment),
            ],
        )
        if subscription is None:
            await callback.answer("Подписка не найдена.", show_alert=True)
            return
        permission = AdminPermissionService(session)
        if not await permission.is_admin(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        scope = await permission.channel_scope(callback.from_user.id)
        if scope is not None and subscription.channel_id not in scope:
            await callback.answer("Подписка недоступна.", show_alert=True)
            return
        can_manage = await permission.can_manage_channel(
            callback.from_user.id,
            subscription.channel_id,
        )
    await callback.answer()
    payment = f"#{subscription.payment_id}" if subscription.payment_id else "нет"
    text = (
        f"Подписка #{subscription.id}\n\n"
        f"Пользователь: {_user_identity(subscription.user)}\n"
        f"Telegram ID: {subscription.user.telegram_id}\n"
        f"Канал: {subscription.channel.title}\n"
        f"Статус: {subscription.status.value}\n"
        f"Источник: {subscription.source}\n"
        f"Начало: {subscription.starts_at:%d.%m.%Y %H:%M}\n"
        f"Текущий период: {subscription.period_started_at:%d.%m.%Y %H:%M}\n"
        f"Окончание: {subscription.expires_at:%d.%m.%Y %H:%M}\n"
        f"Платёж: {payment}"
    )
    rows = [
        [
            InlineKeyboardButton(
                text="Карточка пользователя",
                callback_data=f"adm:accessuser:{subscription.user.telegram_id}",
            )
        ]
    ]
    if can_manage and subscription.status == SubscriptionStatus.ACTIVE:
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="Продлить",
                        callback_data=f"rpt:subextend:{subscription.id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Выдать ссылки повторно",
                        callback_data=f"rpt:subrefresh:{subscription.id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Отозвать",
                        callback_data=f"adm:revoke:{subscription.id}",
                    )
                ],
            ]
        )
    rows.append([InlineKeyboardButton(text="Назад", callback_data="rpt:subs:all:0")])
    await _edit(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("rpt:subextend:"))
async def subscription_extend_start(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    subscription_id = _callback_int(callback, 2)
    if subscription_id is None:
        return
    async with async_session_factory() as session:
        subscription = await session.get(Subscription, subscription_id)
        if (
            subscription is None
            or subscription.status != SubscriptionStatus.ACTIVE
            or not await AdminPermissionService(session).can_manage_channel(
                callback.from_user.id,
                subscription.channel_id,
            )
        ):
            await callback.answer("Продление недоступно.", show_alert=True)
            return
    await state.set_state(ReportActionForm.extend_days)
    await state.update_data(subscription_id=subscription_id)
    await callback.answer()
    await _edit(
        callback,
        "На сколько дней продлить подписку? Введите число от 1 до 3650.",
        admin_cancel_keyboard(f"rpt:sub:{subscription_id}"),
    )


@router.message(ReportActionForm.extend_days)
async def subscription_extend_save(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        await state.clear()
        return
    try:
        days = int((message.text or "").strip())
        if not 1 <= days <= 3650:
            raise ValueError
    except ValueError:
        await message.answer("Введите целое число от 1 до 3650.")
        return
    data = await state.get_data()
    subscription_id = int(data["subscription_id"])
    async with async_session_factory() as session:
        subscription = await session.get(
            Subscription,
            subscription_id,
            options=[selectinload(Subscription.user)],
        )
        if (
            subscription is None
            or subscription.status != SubscriptionStatus.ACTIVE
            or not await AdminPermissionService(session).can_manage_channel(
                message.from_user.id,
                subscription.channel_id,
            )
        ):
            await state.clear()
            await message.answer(
                "Продление недоступно.",
                reply_markup=admin_menu_keyboard(),
            )
            return
        try:
            extended, invite = await SubscriptionService(session).extend_manual(
                subscription.id,
                days,
                message.bot,
            )
        except (TelegramAPIError, ValueError) as exc:
            await session.rollback()
            await message.answer(
                f"Не удалось продлить подписку: {type(exc).__name__}.",
                reply_markup=admin_menu_keyboard(),
            )
            return
        await AdminPermissionService(session).audit(
            message.from_user.id,
            "subscription.extend",
            "subscription",
            extended.id,
            {"days": days},
        )
        await session.commit()
        telegram_id = subscription.user.telegram_id
        delivery_text = access_delivery_text(
            expires_at=extended.expires_at,
            primary_link=invite.link,
            materials_link=invite.materials_link,
        )
    await state.clear()
    delivered = True
    try:
        await message.bot.send_message(telegram_id, delivery_text)
    except TelegramAPIError:
        delivered = False
    await message.answer(
        f"Подписка продлена на {days} дн."
        + (
            " Обе ссылки отправлены пользователю."
            if delivered
            else " Ссылки созданы, но сообщение пользователю не доставлено."
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Вернуться к подписке",
                        callback_data=f"rpt:sub:{subscription_id}",
                    )
                ]
            ]
        ),
    )


@router.callback_query(F.data.startswith("rpt:subrefresh:"))
async def subscription_refresh_links(callback: CallbackQuery) -> None:
    subscription_id = _callback_int(callback, 2)
    if subscription_id is None:
        return
    async with async_session_factory() as session:
        subscription = await session.get(
            Subscription,
            subscription_id,
            options=[selectinload(Subscription.user)],
        )
        if subscription is None or not await AdminPermissionService(
            session
        ).can_manage_channel(callback.from_user.id, subscription.channel_id):
            await callback.answer("Подписка недоступна.", show_alert=True)
            return
        telegram_id = subscription.user.telegram_id
        try:
            refreshed, invite = await SubscriptionService(session).refresh_invite(
                subscription_id,
                callback.bot,
            )
            await session.commit()
        except (TelegramAPIError, ValueError) as exc:
            await session.rollback()
            await callback.answer(str(exc)[:180], show_alert=True)
            return
    try:
        await callback.bot.send_message(
            telegram_id,
            access_delivery_text(
                expires_at=refreshed.expires_at,
                primary_link=invite.link,
                materials_link=invite.materials_link,
            ),
        )
    except TelegramAPIError:
        await callback.answer(
            "Ссылки созданы, но сообщение пользователю не доставлено.",
            show_alert=True,
        )
        return
    await callback.answer("Новые ссылки отправлены пользователю.", show_alert=True)


@router.callback_query(F.data.startswith("rpt:pay:"))
async def payments_report(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if (
        len(parts) != 5
        or parts[2] not in PAYMENT_PROVIDER_TOKENS
        or parts[3] not in PAYMENT_STATUS_TOKENS
    ):
        return
    page = _safe_nonnegative_int(parts[4])
    if page is None:
        return
    async with async_session_factory() as session:
        scope = await _authorized_scope(session, callback.from_user.id)
        if scope is _DENIED:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        statement = _payments_statement(parts[2], parts[3], scope)
        total = await _statement_count(session, statement)
        max_page = max(0, (total - 1) // PAGE_SIZE)
        page = min(page, max_page)
        payments = list(
            await session.scalars(
                statement.options(
                    selectinload(Payment.user),
                    selectinload(Payment.plan).selectinload(Plan.channel),
                )
                .order_by(Payment.id.desc())
                .offset(page * PAGE_SIZE)
                .limit(PAGE_SIZE)
            )
        )
    await callback.answer()
    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"#{payment.id} | {_user_identity(payment.user)} | "
                    f"{payment.provider}/{payment.status.value}"
                )[:64],
                callback_data=f"rpt:payment:{payment.id}",
            )
        ]
        for payment in payments
    ]
    rows.extend(_payment_filter_rows(parts[2], parts[3]))
    navigation = _navigation(
        page,
        max_page,
        lambda target: f"rpt:pay:{parts[2]}:{parts[3]}:{target}",
    )
    if navigation:
        rows.append(navigation)
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="Экспорт XLSX",
                    callback_data=f"rpt:export:pay:{parts[2]}:{parts[3]}",
                )
            ],
            [InlineKeyboardButton(text="В меню", callback_data="admin:menu")],
        ]
    )
    await _edit(
        callback,
        f"Платежи\n\nПровайдер: {_provider_label(parts[2])}\n"
        f"Статус: {_payment_status_label(parts[3])}\n"
        f"Найдено: {total}\nСтраница {page + 1} из {max_page + 1}",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("rpt:payment:"))
async def payment_card(callback: CallbackQuery) -> None:
    payment_id = _callback_int(callback, 2)
    if payment_id is None:
        return
    async with async_session_factory() as session:
        payment = await session.get(
            Payment,
            payment_id,
            options=[
                selectinload(Payment.user),
                selectinload(Payment.plan).selectinload(Plan.channel),
                selectinload(Payment.subscription),
            ],
        )
        if payment is None:
            await callback.answer("Платёж не найден.", show_alert=True)
            return
        permission = AdminPermissionService(session)
        if not await permission.is_admin(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        scope = await permission.channel_scope(callback.from_user.id)
        if scope is not None and payment.plan.channel_id not in scope:
            await callback.answer("Платёж недоступен.", show_alert=True)
            return
        can_manage = await permission.can_manage_channel(
            callback.from_user.id,
            payment.plan.channel_id,
        )
    await callback.answer()
    external_id = _masked_external_id(payment.provider_payment_id)
    text = (
        f"Платёж #{payment.id}\n\n"
        f"Пользователь: {_user_identity(payment.user)}\n"
        f"Telegram ID: {payment.user.telegram_id}\n"
        f"Тариф: {payment.plan.title}\n"
        f"Канал: {payment.plan.channel.title}\n"
        f"Провайдер: {payment.provider}\n"
        f"Сумма: {payment.amount:g} {payment.currency}\n"
        f"Статус: {payment.status.value}\n"
        f"Создан: {payment.created_at:%d.%m.%Y %H:%M}\n"
        f"Оплачен: {_format_dt(payment.paid_at)}\n"
        f"Внешний ID: {external_id}\n"
        f"Подписка: {f'#{payment.subscription.id}' if payment.subscription else 'нет'}"
    )
    rows = [
        [
            InlineKeyboardButton(
                text="Карточка пользователя",
                callback_data=f"adm:accessuser:{payment.user.telegram_id}",
            )
        ]
    ]
    if can_manage and payment.status == PaymentStatus.PAID:
        if payment.subscription is None:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="Повторить выдачу доступа",
                        callback_data=f"rpt:provision:{payment.id}",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="Оформить возврат",
                    callback_data=f"rpt:refundask:{payment.id}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="Назад", callback_data="rpt:pay:all:all:0")]
    )
    await _edit(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("rpt:provision:"))
async def payment_retry_provision(callback: CallbackQuery) -> None:
    payment_id = _callback_int(callback, 2)
    if payment_id is None:
        return
    async with async_session_factory() as session:
        payment = await session.get(
            Payment,
            payment_id,
            options=[
                selectinload(Payment.user),
                selectinload(Payment.plan),
                selectinload(Payment.subscription),
            ],
        )
        if (
            payment is None
            or payment.status != PaymentStatus.PAID
            or payment.subscription is not None
            or not await AdminPermissionService(session).can_manage_channel(
                callback.from_user.id,
                payment.plan.channel_id,
            )
        ):
            await callback.answer("Повторная выдача недоступна.", show_alert=True)
            return
        try:
            async with session.begin_nested():
                subscription, invite, materials = await SubscriptionService(
                    session
                ).activate_from_payment(payment, callback.bot)
        except (TelegramAPIError, ValueError) as exc:
            await session.rollback()
            await callback.answer(
                f"Выдача не выполнена: {type(exc).__name__}. Проверьте права бота.",
                show_alert=True,
            )
            return
        await AdminPermissionService(session).audit(
            callback.from_user.id,
            "payment.provision.retry",
            "payment",
            payment.id,
            {"subscription_id": subscription.id},
        )
        await session.commit()
        telegram_id = payment.user.telegram_id
        delivery_text = (
            "Оплата подтверждена.\n\n"
            + access_delivery_text(
                expires_at=subscription.expires_at,
                primary_link=invite.link,
                materials_link=materials,
            )
        )
    delivered = True
    try:
        await callback.bot.send_message(telegram_id, delivery_text)
    except TelegramAPIError:
        delivered = False
    await callback.answer(
        "Доступ выдан"
        + (
            " и ссылки отправлены пользователю."
            if delivered
            else ", но сообщение пользователю не доставлено."
        ),
        show_alert=True,
    )


@router.callback_query(F.data.startswith("rpt:refundask:"))
async def payment_refund_prompt(callback: CallbackQuery) -> None:
    payment_id = _callback_int(callback, 2)
    if payment_id is None:
        return
    async with async_session_factory() as session:
        payment = await session.get(
            Payment,
            payment_id,
            options=[selectinload(Payment.plan)],
        )
        if (
            payment is None
            or payment.status != PaymentStatus.PAID
            or not await AdminPermissionService(session).can_manage_channel(
                callback.from_user.id,
                payment.plan.channel_id,
            )
        ):
            await callback.answer("Возврат недоступен.", show_alert=True)
            return
    await callback.answer()
    await _edit(
        callback,
        f"Вернуть {payment.amount:g} {payment.currency} по платежу #{payment.id}? "
        "После возврата доступ будет отозван.",
        refund_confirmation_keyboard(payment.id),
    )


@router.callback_query(F.data.startswith("rpt:export:"))
async def export_report(callback: CallbackQuery) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) not in {4, 5}:
        return
    report_type = parts[2]
    async with async_session_factory() as session:
        scope = await _authorized_scope(session, callback.from_user.id)
        if scope is _DENIED:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        if report_type == "users" and len(parts) == 4:
            statement = _users_statement(parts[3], scope)
            users = list(
                await session.scalars(
                    statement.order_by(User.id.desc()).limit(MAX_EXPORT_ROWS + 1)
                )
            )
            data = build_xlsx(
                sheet_name="Пользователи",
                headers=[
                    "Telegram ID",
                    "Username",
                    "Имя",
                    "Фамилия",
                    "Создан",
                    "Обновлён",
                ],
                rows=[
                    (
                        user.telegram_id,
                        user.username or "",
                        user.first_name or "",
                        user.last_name or "",
                        user.created_at,
                        user.updated_at,
                    )
                    for user in users[:MAX_EXPORT_ROWS]
                ],
            )
            filename = "propgate_users.xlsx"
            filter_meta = parts[3]
        elif report_type == "subs" and len(parts) == 4:
            statement = _subscriptions_statement(parts[3], scope)
            subscriptions = list(
                await session.scalars(
                    statement.options(
                        selectinload(Subscription.user),
                        selectinload(Subscription.channel),
                    )
                    .order_by(Subscription.id.desc())
                    .limit(MAX_EXPORT_ROWS + 1)
                )
            )
            data = build_xlsx(
                sheet_name="Подписки",
                headers=[
                    "ID",
                    "Telegram ID",
                    "Username",
                    "Канал",
                    "Статус",
                    "Источник",
                    "Начало",
                    "Начало периода",
                    "Окончание",
                    "Платёж ID",
                ],
                rows=[
                    (
                        item.id,
                        item.user.telegram_id,
                        item.user.username or "",
                        item.channel.title,
                        item.status.value,
                        item.source,
                        item.starts_at,
                        item.period_started_at,
                        item.expires_at,
                        item.payment_id or "",
                    )
                    for item in subscriptions[:MAX_EXPORT_ROWS]
                ],
            )
            users = subscriptions
            filename = "propgate_subscriptions.xlsx"
            filter_meta = parts[3]
        elif report_type == "pay" and len(parts) == 5:
            provider_token, status_token = parts[3], parts[4]
            if (
                provider_token not in PAYMENT_PROVIDER_TOKENS
                or status_token not in PAYMENT_STATUS_TOKENS
            ):
                return
            statement = _payments_statement(provider_token, status_token, scope)
            payments = list(
                await session.scalars(
                    statement.options(
                        selectinload(Payment.user),
                        selectinload(Payment.plan).selectinload(Plan.channel),
                    )
                    .order_by(Payment.id.desc())
                    .limit(MAX_EXPORT_ROWS + 1)
                )
            )
            data = build_xlsx(
                sheet_name="Платежи",
                headers=[
                    "ID",
                    "Telegram ID",
                    "Username",
                    "Канал",
                    "Тариф",
                    "Провайдер",
                    "Статус",
                    "Сумма",
                    "Валюта",
                    "Создан",
                    "Оплачен",
                    "Возвращён",
                    "Внешний ID",
                ],
                rows=[
                    (
                        item.id,
                        item.user.telegram_id,
                        item.user.username or "",
                        item.plan.channel.title,
                        item.plan.title,
                        item.provider,
                        item.status.value,
                        item.amount,
                        item.currency,
                        item.created_at,
                        item.paid_at or "",
                        item.refunded_at or "",
                        _masked_external_id(item.provider_payment_id),
                    )
                    for item in payments[:MAX_EXPORT_ROWS]
                ],
            )
            users = payments
            filename = f"propgate_payments_{provider_token}_{status_token}.xlsx"
            filter_meta = f"{provider_token}/{status_token}"
        else:
            return
        await AdminPermissionService(session).audit(
            callback.from_user.id,
            "report.export",
            report_type,
            metadata={
                "filter": filter_meta,
                "rows": min(len(users), MAX_EXPORT_ROWS),
                "truncated": len(users) > MAX_EXPORT_ROWS,
            },
        )
        await session.commit()
    await callback.answer("Формирую XLSX.")
    if callback.message:
        await callback.message.answer_document(
            BufferedInputFile(data, filename=filename),
            caption=(
                f"Экспорт готов. Строк: {min(len(users), MAX_EXPORT_ROWS)}"
                + (
                    f". Выгрузка ограничена {MAX_EXPORT_ROWS} строками."
                    if len(users) > MAX_EXPORT_ROWS
                    else ""
                )
            ),
        )


class _Denied:
    pass


_DENIED = _Denied()


async def _authorized_scope(
    session: AsyncSession,
    telegram_id: int,
) -> list[int] | None | _Denied:
    permission = AdminPermissionService(session)
    if not await permission.is_admin(telegram_id):
        return _DENIED
    return await permission.channel_scope(telegram_id)


def _users_statement(
    filter_token: str,
    scope: list[int] | None,
):
    now = datetime.now(UTC).replace(tzinfo=None)
    active_conditions = [
        Subscription.user_id == User.id,
        Subscription.status == SubscriptionStatus.ACTIVE,
        Subscription.expires_at > now,
    ]
    scoped_conditions = [Subscription.user_id == User.id]
    if scope is not None:
        if not scope:
            return select(User).where(false())
        active_conditions.append(Subscription.channel_id.in_(scope))
        scoped_conditions.append(Subscription.channel_id.in_(scope))
    active_exists = exists(select(Subscription.id).where(*active_conditions))
    statement = select(User)
    if scope is not None:
        statement = statement.where(
            exists(select(Subscription.id).where(*scoped_conditions))
        )
    if filter_token == "active":
        statement = statement.where(active_exists)
    elif filter_token == "inactive":
        statement = statement.where(~active_exists)
    elif filter_token in {"24h", "3d"}:
        delta = timedelta(hours=24) if filter_token == "24h" else timedelta(days=3)
        statement = statement.where(
            exists(
                select(Subscription.id).where(
                    *active_conditions,
                    Subscription.expires_at <= now + delta,
                )
            )
        )
    elif filter_token.startswith("c") and filter_token[1:].isdigit():
        channel_id = int(filter_token[1:])
        if scope is not None and channel_id not in scope:
            return select(User).where(false())
        statement = statement.where(
            exists(
                select(Subscription.id).where(
                    Subscription.user_id == User.id,
                    Subscription.channel_id == channel_id,
                )
            )
        )
    elif filter_token != "all":
        return select(User).where(false())
    return statement


def _subscriptions_statement(
    filter_token: str,
    scope: list[int] | None,
):
    now = datetime.now(UTC).replace(tzinfo=None)
    statement = select(Subscription)
    if scope is not None:
        statement = (
            statement.where(Subscription.channel_id.in_(scope))
            if scope
            else statement.where(false())
        )
    if filter_token == "active":
        statement = statement.where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.expires_at > now,
        )
    elif filter_token == "expiring":
        statement = statement.where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.expires_at > now,
            Subscription.expires_at <= now + timedelta(days=3),
        )
    elif filter_token == "expired":
        statement = statement.where(
            or_(
                Subscription.status == SubscriptionStatus.EXPIRED,
                and_(
                    Subscription.status == SubscriptionStatus.ACTIVE,
                    Subscription.expires_at <= now,
                ),
            )
        )
    elif filter_token == "cancelled":
        statement = statement.where(
            Subscription.status == SubscriptionStatus.CANCELLED
        )
    elif filter_token != "all":
        statement = statement.where(false())
    return statement


def _payments_statement(
    provider_token: str,
    status_token: str,
    scope: list[int] | None,
):
    statement = select(Payment)
    if scope is not None:
        statement = (
            statement.join(Payment.plan).where(Plan.channel_id.in_(scope))
            if scope
            else statement.where(false())
        )
    provider = PAYMENT_PROVIDER_TOKENS.get(provider_token)
    if provider:
        statement = statement.where(Payment.provider == provider)
    if status_token == "attention":
        statement = statement.where(
            or_(
                Payment.status == PaymentStatus.FAILED,
                and_(
                    Payment.status == PaymentStatus.PAID,
                    ~exists(
                        select(Subscription.id).where(
                            Subscription.payment_id == Payment.id
                        )
                    ),
                ),
            )
        )
    elif status_token != "all":
        statement = statement.where(Payment.status == PaymentStatus(status_token))
    return statement


async def _statement_count(session: AsyncSession, statement) -> int:
    subquery = statement.order_by(None).subquery()
    return int(await session.scalar(select(func.count()).select_from(subquery)) or 0)


def _user_filter_rows(current: str) -> list[list[InlineKeyboardButton]]:
    return [
        [
            _filter_button("Все", "all", current, "rpt:users:{value}:0"),
            _filter_button("Активные", "active", current, "rpt:users:{value}:0"),
        ],
        [
            _filter_button("Без подписки", "inactive", current, "rpt:users:{value}:0"),
            _filter_button("≤24 ч", "24h", current, "rpt:users:{value}:0"),
            _filter_button("≤3 дн", "3d", current, "rpt:users:{value}:0"),
        ],
    ]


def _subscription_filter_rows(current: str) -> list[list[InlineKeyboardButton]]:
    return [
        [
            _filter_button("Все", "all", current, "rpt:subs:{value}:0"),
            _filter_button("Активные", "active", current, "rpt:subs:{value}:0"),
        ],
        [
            _filter_button("Истекают", "expiring", current, "rpt:subs:{value}:0"),
            _filter_button("Истёкшие", "expired", current, "rpt:subs:{value}:0"),
        ],
        [
            _filter_button("Отменённые", "cancelled", current, "rpt:subs:{value}:0"),
        ],
    ]


def _payment_filter_rows(
    provider: str,
    status: str,
) -> list[list[InlineKeyboardButton]]:
    return [
        [
            _payment_filter_button("Все", "all", provider, status, provider=True),
            _payment_filter_button("Stars", "stars", provider, status, provider=True),
            _payment_filter_button("Crypto", "crypto", provider, status, provider=True),
            _payment_filter_button("ЮKassa", "yoo", provider, status, provider=True),
        ],
        [
            _payment_filter_button("Все статусы", "all", provider, status),
            _payment_filter_button("Ожидают", "pending", provider, status),
        ],
        [
            _payment_filter_button("Оплачены", "paid", provider, status),
            _payment_filter_button("Ошибки", "failed", provider, status),
            _payment_filter_button("Истекли", "expired", provider, status),
        ],
        [
            _payment_filter_button("Возвраты", "refunded", provider, status),
            _payment_filter_button("Требуют внимания", "attention", provider, status),
        ],
    ]


def _filter_button(
    label: str,
    value: str,
    current: str,
    callback_template: str,
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=f"{'✓ ' if current == value else ''}{label}",
        callback_data=callback_template.format(value=value),
    )


def _payment_filter_button(
    label: str,
    value: str,
    current_provider: str,
    current_status: str,
    *,
    provider: bool = False,
) -> InlineKeyboardButton:
    selected = current_provider == value if provider else current_status == value
    target_provider = value if provider else current_provider
    target_status = current_status if provider else value
    return InlineKeyboardButton(
        text=f"{'✓ ' if selected else ''}{label}",
        callback_data=f"rpt:pay:{target_provider}:{target_status}:0",
    )


def _navigation(page: int, max_page: int, callback_builder) -> list[InlineKeyboardButton]:
    buttons: list[InlineKeyboardButton] = []
    if page > 0:
        buttons.append(
            InlineKeyboardButton(
                text="Назад",
                callback_data=callback_builder(page - 1),
            )
        )
    if page < max_page:
        buttons.append(
            InlineKeyboardButton(
                text="Далее",
                callback_data=callback_builder(page + 1),
            )
        )
    return buttons


def _callback_int(callback: CallbackQuery, index: int) -> int | None:
    parts = (callback.data or "").split(":")
    if len(parts) <= index:
        return None
    try:
        return int(parts[index])
    except ValueError:
        return None


def _safe_nonnegative_int(value: str) -> int | None:
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _user_identity(user: User) -> str:
    if user.username:
        return f"@{user.username}"
    name = " ".join(
        part for part in (user.first_name, user.last_name) if part
    ).strip()
    return name or f"ID {user.telegram_id}"


def _masked_external_id(value: str | None) -> str:
    if not value:
        return "нет"
    if len(value) <= 10:
        return value
    return f"{value[:6]}…{value[-4:]}"


def _format_dt(value: datetime | None) -> str:
    return value.strftime("%d.%m.%Y %H:%M") if value else "—"


def _user_filter_label(value: str) -> str:
    labels = {
        "all": "все",
        "active": "активная подписка",
        "inactive": "без активной подписки",
        "24h": "истекают за 24 часа",
        "3d": "истекают за 3 дня",
    }
    return labels.get(value, "по каналу" if value.startswith("c") else value)


def _subscription_filter_label(value: str) -> str:
    return {
        "all": "все",
        "active": "активные",
        "expiring": "истекающие",
        "expired": "истёкшие",
        "cancelled": "отменённые",
    }[value]


def _provider_label(value: str) -> str:
    return {
        "all": "все",
        "stars": "Telegram Stars",
        "crypto": "Crypto Pay",
        "yoo": "ЮKassa",
    }[value]


def _payment_status_label(value: str) -> str:
    return {
        "all": "все",
        "pending": "ожидают",
        "paid": "оплачены",
        "failed": "ошибки",
        "expired": "истекли",
        "refunded": "возвращены",
        "attention": "требуют внимания",
    }[value]


async def _edit(
    callback: CallbackQuery,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    if callback.message:
        await callback.message.edit_text(text, reply_markup=keyboard)
