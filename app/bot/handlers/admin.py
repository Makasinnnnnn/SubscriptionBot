from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.admin.service import AdminPermissionService
from app.admin.settings_service import RuntimeSettingsService
from app.bot.keyboards.admin import (
    admin_back_keyboard,
    admin_menu_keyboard,
    refund_confirmation_keyboard,
)
from app.core.config import get_settings
from app.db.enums import PaymentStatus, SubscriptionStatus
from app.db.models import (
    Admin,
    AdminChannelAccess,
    AuditLog,
    Channel,
    NotificationLog,
    Payment,
    Plan,
    Subscription,
    User,
    WebhookEvent,
)
from app.db.session import async_session_factory
from app.payments.providers import ProviderAPIError, ProviderNotConfiguredError
from app.payments.registry import get_payment_provider
from app.services.payment_service import PaymentService, set_status_timestamp
from app.subscriptions.service import SubscriptionService
from app.subscriptions.presentation import access_delivery_text

router = Router()


@router.message(Command("admin"))
async def admin_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with async_session_factory() as session:
        if not await _authorize_message(message, session):
            return
    await message.answer(
        "Управление PropGate\n\nВыберите раздел:",
        reply_markup=admin_menu_keyboard(),
    )


@router.callback_query(F.data == "admin:menu")
async def admin_menu_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not await _authorize_callback(callback):
        return
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "Управление PropGate\n\nВыберите раздел:",
            reply_markup=admin_menu_keyboard(),
        )


@router.callback_query(F.data.startswith("admin:"))
async def admin_section_callback(callback: CallbackQuery) -> None:
    if not await _authorize_callback(callback):
        return
    await callback.answer()
    if callback.message is None or callback.data is None:
        return
    section = callback.data.partition(":")[2]
    if section == "subscriptions":
        text, keyboard = await _subscriptions_view(callback.from_user.id)
        await callback.message.edit_text(text, reply_markup=keyboard)
        return
    text = await _section_text(section, callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=admin_back_keyboard())


@router.message(Command("admin_channels"))
async def admin_channels(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session):
            return
        await message.answer(await _channels_text(session, message.from_user.id))


@router.message(Command("admin_add_channel"))
async def admin_add_channel(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, manage=True):
            return
        parts = _pipe_payload(message)
        if len(parts) < 2:
            await message.answer("Формат: /admin_add_channel chat_id | Название | описание")
            return
        try:
            telegram_chat_id = int(parts[0])
        except ValueError:
            await message.answer("chat_id должен быть числом, например -1001234567890.")
            return

        if not await _bot_is_channel_admin(message, telegram_chat_id):
            await message.answer("Бот не является администратором этого канала или канал недоступен.")
            return

        channel = await session.scalar(
            select(Channel).where(Channel.telegram_chat_id == telegram_chat_id)
        )
        permission_service = AdminPermissionService(session)
        if channel is None:
            allowed = await permission_service.can_manage_all_channels(message.from_user.id)
        else:
            allowed = await permission_service.can_manage_channel(message.from_user.id, channel.id)
        if not allowed:
            await message.answer("У тебя нет прав на создание или изменение этого канала.")
            return
        if channel is None:
            channel = Channel(telegram_chat_id=telegram_chat_id, title=parts[1])
            session.add(channel)
        channel.title = parts[1]
        channel.description = parts[2] if len(parts) > 2 and parts[2] else None
        channel.is_active = True
        await session.flush()
        await _audit(session, message, "channel.save", "channel", channel.id)
        await session.commit()
        await message.answer(f"Канал сохранён: {channel.title} (ID {channel.id}).")


@router.message(Command("admin_disable_channel"))
async def admin_disable_channel(message: Message) -> None:
    await _set_channel_active(message, False)


@router.message(Command("admin_enable_channel"))
async def admin_enable_channel(message: Message) -> None:
    await _set_channel_active(message, True)


@router.message(Command("admin_plans"))
async def admin_plans(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session):
            return
        await message.answer(await _plans_text(session, message.from_user.id))


@router.message(Command("admin_add_plan"))
async def admin_add_plan(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, manage=True):
            return
        parts = _pipe_payload(message)
        if len(parts) < 4:
            await message.answer(
                "Формат: /admin_add_plan channel_id | Название | дни | цена | RUB"
            )
            return
        try:
            channel_id = int(parts[0])
            duration_days = int(parts[2])
            price = Decimal(parts[3])
        except (ValueError, InvalidOperation):
            await message.answer("channel_id и дни должны быть целыми числами, цена — числом.")
            return
        if duration_days <= 0 or price <= 0:
            await message.answer("Дни и цена должны быть больше нуля.")
            return
        channel = await session.get(Channel, channel_id)
        if channel is None:
            await message.answer("Канал не найден. Сначала добавь его через /admin_add_channel.")
            return
        if not await _authorize_channel(message, session, channel.id):
            return
        currency = parts[4].upper() if len(parts) > 4 and parts[4] else "RUB"
        plan = Plan(
            channel_id=channel.id,
            title=parts[1],
            duration_days=duration_days,
            price=price,
            currency=currency,
            is_active=True,
        )
        session.add(plan)
        await session.flush()
        await _audit(session, message, "plan.create", "plan", plan.id)
        await session.commit()
        await message.answer(f"Тариф сохранён: {plan.title}, {price} {currency} (ID {plan.id}).")


@router.message(Command("admin_disable_plan"))
async def admin_disable_plan(message: Message) -> None:
    await _set_plan_active(message, False)


@router.message(Command("admin_enable_plan"))
async def admin_enable_plan(message: Message) -> None:
    await _set_plan_active(message, True)


@router.message(Command("admin_payments"))
async def admin_payments(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session):
            return
        await message.answer(
            await _payments_text(session, _limit(message), message.from_user.id)
        )


@router.message(Command("admin_refresh_payment"))
async def admin_refresh_payment(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, manage=True):
            return
        try:
            payment_id = int(_command_payload(message))
        except ValueError:
            await message.answer("Формат: /admin_refresh_payment payment_id")
            return
        payment = await session.get(Payment, payment_id, options=[selectinload(Payment.plan)])
        if payment is None:
            await message.answer("Платёж не найден.")
            return
        if not await _authorize_channel(message, session, payment.plan.channel_id):
            return
        if payment.provider in {"dev", "telegram-stars"}:
            await message.answer("Статус этого провайдера обновляется только событием внутри бота.")
            return
        try:
            payment = await PaymentService(session).refresh_status(payment)
            if payment.status == PaymentStatus.PAID:
                await SubscriptionService(session).activate_from_payment(payment, message.bot)
        except (ProviderNotConfiguredError, ProviderAPIError, ValueError) as exc:
            await message.answer(f"Не удалось обновить платёж: {exc}")
            return
        await _audit(
            session,
            message,
            "payment.refresh",
            "payment",
            payment.id,
            {"status": payment.status.value},
        )
        await session.commit()
        await message.answer(f"Статус платежа #{payment.id}: {payment.status.value}.")


@router.message(Command("admin_refund"))
async def admin_refund(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, manage=True):
            return
        try:
            payment_id = int(_command_payload(message))
        except ValueError:
            await message.answer("Формат: /admin_refund payment_id")
            return
        payment = await session.get(Payment, payment_id, options=[selectinload(Payment.plan)])
        if payment is None or payment.status != PaymentStatus.PAID:
            await message.answer("Для полного возврата нужен существующий платёж в статусе paid.")
            return
        if not await _authorize_channel(message, session, payment.plan.channel_id):
            return
        await message.answer(
            f"Вернуть полностью {payment.amount} {payment.currency} по платежу #{payment.id}? "
            "После возврата доступ будет отозван.",
            reply_markup=refund_confirmation_keyboard(payment.id),
        )


@router.callback_query(F.data.startswith("adminrefund:"))
async def admin_refund_confirm(callback: CallbackQuery) -> None:
    if callback.data is None or callback.message is None:
        return
    async with async_session_factory() as session:
        permission = AdminPermissionService(session)
        if not await permission.can_manage(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        try:
            payment_id = int(callback.data.split(":", maxsplit=1)[1])
        except ValueError:
            await callback.answer("Некорректный платёж.", show_alert=True)
            return
        payment = await session.scalar(
            select(Payment)
            .options(selectinload(Payment.plan), selectinload(Payment.user))
            .where(Payment.id == payment_id)
            .with_for_update()
        )
        if payment is None or payment.status != PaymentStatus.PAID:
            await callback.answer("Платёж уже изменён или не найден.", show_alert=True)
            return
        if not await permission.can_manage_channel(callback.from_user.id, payment.plan.channel_id):
            await callback.answer("Нет прав на этот канал.", show_alert=True)
            return

        try:
            refunded = await _refund_payment(callback, payment)
        except (ProviderNotConfiguredError, ProviderAPIError, TelegramAPIError):
            await callback.answer("Провайдер не подтвердил возврат.", show_alert=True)
            return
        if not refunded:
            await callback.answer("Этот провайдер не поддерживает автоматический возврат.", show_alert=True)
            return

        payment.status = PaymentStatus.REFUNDED
        set_status_timestamp(payment, PaymentStatus.REFUNDED)
        subscription = await session.scalar(
            select(Subscription).where(
                Subscription.payment_id == payment.id,
                Subscription.status == SubscriptionStatus.ACTIVE,
            )
        )
        if subscription is not None:
            await SubscriptionService(session).revoke(
                subscription.id,
                callback.bot,
                reason=f"payment {payment.id} refunded",
            )
        await permission.audit(
            callback.from_user.id,
            "payment.refund",
            "payment",
            payment.id,
            {"amount": str(payment.amount), "currency": payment.currency},
        )
        await session.commit()
    await callback.answer("Возврат выполнен.")
    await callback.message.edit_text(
        f"Платёж #{payment_id} возвращён, связанный доступ отозван.",
        reply_markup=admin_back_keyboard(),
    )


@router.message(Command("admin_subscriptions"))
async def admin_subscriptions(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session):
            return
        await message.answer(
            await _subscriptions_text(session, _limit(message), message.from_user.id)
        )


@router.message(Command("admin_user"))
async def admin_user(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session):
            return
        try:
            telegram_id = int(_command_payload(message))
        except ValueError:
            await message.answer("Формат: /admin_user telegram_id")
            return
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            await message.answer("Пользователь не найден.")
            return
        scope = await AdminPermissionService(session).channel_scope(message.from_user.id)
        subscriptions_query = (
            select(Subscription)
            .options(selectinload(Subscription.channel))
            .where(Subscription.user_id == user.id)
            .order_by(Subscription.expires_at.desc())
        )
        payments_count_query = (
            select(func.count(Payment.id))
            .join(Payment.plan)
            .where(Payment.user_id == user.id)
        )
        if scope is not None:
            subscriptions_query = subscriptions_query.where(
                Subscription.channel_id.in_(scope)
            )
            payments_count_query = payments_count_query.where(Plan.channel_id.in_(scope))
        subscriptions = list(await session.scalars(subscriptions_query))
        payments_count = await session.scalar(payments_count_query) or 0
        lines = [
            f"Пользователь #{user.id}",
            f"Telegram ID: {user.telegram_id}",
            f"Username: @{user.username}" if user.username else "Username: —",
            f"Платежей: {payments_count}",
            f"Подписок: {len(subscriptions)}",
        ]
        for subscription in subscriptions[:10]:
            lines.append(
                f"#{subscription.id} {subscription.channel.title}: {subscription.status.value}, "
                f"до {_format_dt(subscription.expires_at)}"
            )
        await message.answer("\n".join(lines))


@router.message(Command("admin_grant"))
async def admin_grant(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, manage=True):
            return
        parts = _pipe_payload(message)
        if len(parts) != 3:
            await message.answer("Формат: /admin_grant telegram_id | channel_id | days")
            return
        try:
            telegram_id, channel_id, days = (int(part) for part in parts)
            if not await _authorize_channel(message, session, channel_id):
                return
            subscription, invite = await SubscriptionService(session).grant_manual(
                telegram_id,
                channel_id,
                days,
                message.bot,
            )
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await _audit(
            session,
            message,
            "subscription.grant",
            "subscription",
            subscription.id,
            {"telegram_id": telegram_id, "channel_id": channel_id, "days": days},
        )
        await session.commit()
        if invite.materials_link:
            try:
                await message.bot.send_message(
                    telegram_id,
                    access_delivery_text(
                        expires_at=subscription.expires_at,
                        primary_link=invite.link,
                        materials_link=invite.materials_link,
                    ),
                )
            except TelegramAPIError:
                delivery = " Методички: отправка не удалась."
            else:
                await message.answer("Доступ к обоим каналам выдан и отправлен одним сообщением.")
            return
        try:
            await message.bot.send_message(
                telegram_id,
                (
                    "Вам выдан доступ к каналу.\n"
                    f"Подписка активна до {_format_dt(subscription.expires_at)}.\n"
                    f"Ссылка для входа: {invite.link}"
                ),
            )
            delivery = " Ссылка отправлена пользователю."
        except TelegramAPIError:
            delivery = " Не удалось написать пользователю; передай ссылку вручную."
        await message.answer(
            f"Доступ выдан до {_format_dt(subscription.expires_at)}.\n{invite.link}\n{delivery}"
        )


@router.message(Command("admin_revoke"))
async def admin_revoke(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, manage=True):
            return
        try:
            subscription_id = int(_command_payload(message))
            target = await session.get(Subscription, subscription_id)
            if target is None:
                raise ValueError("Subscription not found")
            if not await _authorize_channel(message, session, target.channel_id):
                return
            result = await SubscriptionService(session).revoke(subscription_id, message.bot)
        except ValueError as exc:
            await message.answer(f"Ошибка: {exc}")
            return
        await _audit(
            session,
            message,
            "subscription.revoke",
            "subscription",
            subscription_id,
            {"telegram_changed": result.telegram_access_changed, "error": result.error},
        )
        await session.commit()
        await message.answer(_revoke_result_text(result.telegram_access_changed, result.error))


@router.message(Command("admin_revoke_user"))
async def admin_revoke_user(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, manage=True):
            return
        parts = _pipe_payload(message)
        if len(parts) != 2:
            await message.answer("Формат: /admin_revoke_user telegram_id | channel_id")
            return
        try:
            telegram_id, channel_id = (int(part) for part in parts)
            if not await _authorize_channel(message, session, channel_id):
                return
            result = await SubscriptionService(session).revoke_user(
                telegram_id,
                channel_id,
                message.bot,
            )
        except ValueError as exc:
            await message.answer(f"Ошибка: {exc}")
            return
        await _audit(
            session,
            message,
            "subscription.revoke_user",
            "subscription",
            result.subscription.id,
            {"telegram_id": telegram_id, "channel_id": channel_id, "error": result.error},
        )
        await session.commit()
        await message.answer(_revoke_result_text(result.telegram_access_changed, result.error))


@router.message(Command("admin_stats"))
async def admin_stats(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session):
            return
        await message.answer(await _stats_text(session, message.from_user.id))


@router.message(Command("admin_admins"))
async def admin_admins(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, superadmin=True):
            return
        await message.answer(await _admins_text(session))


@router.message(Command("admin_settings"))
async def admin_settings(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, superadmin=True):
            return
        runtime = RuntimeSettingsService(session)
        settings = get_settings()
        days = await runtime.notification_days(settings.notification_days)
        invite_ttl = await runtime.invite_ttl_minutes()
        scheduler_interval = await runtime.scheduler_interval_minutes(
            settings.scheduler_interval_minutes
        )
        await message.answer(
            "Настройки:\n"
            f"Напоминания: {', '.join(map(str, days))} дн.\n"
            f"Срок invite-ссылки: {invite_ttl} мин.\n"
            f"Интервал scheduler: {scheduler_interval} мин.\n"
            f"Dev-payment: {'включён' if settings.enable_dev_payments else 'выключен через .env'}"
        )


@router.message(Command("admin_set_notification_days"))
async def admin_set_notification_days(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, superadmin=True):
            return
        try:
            raw = _command_payload(message).replace(" ", "")
            days = sorted({int(item) for item in raw.split(",") if item}, reverse=True)
            if not days or any(day <= 0 or day > 365 for day in days):
                raise ValueError
        except ValueError:
            await message.answer("Формат: /admin_set_notification_days 7,3,1")
            return
        await RuntimeSettingsService(session).set(
            RuntimeSettingsService.NOTIFICATION_DAYS,
            days,
            message.from_user.id,
        )
        await _audit(session, message, "settings.notification_days", "setting", None, {"days": days})
        await session.commit()
        await message.answer(f"Напоминания настроены за {', '.join(map(str, days))} дн.")


@router.message(Command("admin_set_invite_ttl"))
async def admin_set_invite_ttl(message: Message) -> None:
    await _set_integer_setting(
        message,
        RuntimeSettingsService.INVITE_TTL_MINUTES,
        5,
        10080,
        "мин.",
    )


@router.message(Command("admin_set_scheduler_interval"))
async def admin_set_scheduler_interval(message: Message) -> None:
    await _set_integer_setting(
        message,
        RuntimeSettingsService.SCHEDULER_INTERVAL_MINUTES,
        1,
        1440,
        "мин.",
    )


@router.message(Command("admin_set_admin_scope"))
async def admin_set_admin_scope(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, superadmin=True):
            return
        parts = _pipe_payload(message)
        if len(parts) != 2 or parts[1].lower() not in {"all", "restricted"}:
            await message.answer("Формат: /admin_set_admin_scope telegram_id | all|restricted")
            return
        try:
            telegram_id = int(parts[0])
            admin = await AdminPermissionService(session).set_all_channels(
                telegram_id,
                parts[1].lower() == "all",
            )
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await _audit(
            session,
            message,
            "admin.scope",
            "admin",
            admin.id,
            {"scope": parts[1].lower()},
        )
        await session.commit()
        await message.answer(f"Область доступа {telegram_id}: {parts[1].lower()}.")


@router.message(Command("admin_grant_admin_channel"))
async def admin_grant_admin_channel(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, superadmin=True):
            return
        parts = _pipe_payload(message)
        if len(parts) != 2:
            await message.answer("Формат: /admin_grant_admin_channel telegram_id | channel_id")
            return
        try:
            telegram_id, channel_id = (int(part) for part in parts)
            if await session.get(Channel, channel_id) is None:
                raise ValueError("Channel not found")
            access = await AdminPermissionService(session).grant_channel(telegram_id, channel_id)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await _audit(
            session,
            message,
            "admin.channel.grant",
            "admin_channel_access",
            access.id,
            {"telegram_id": telegram_id, "channel_id": channel_id},
        )
        await session.commit()
        await message.answer("Доступ администратора к каналу выдан.")


@router.message(Command("admin_revoke_admin_channel"))
async def admin_revoke_admin_channel(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, superadmin=True):
            return
        parts = _pipe_payload(message)
        if len(parts) != 2:
            await message.answer("Формат: /admin_revoke_admin_channel telegram_id | channel_id")
            return
        try:
            telegram_id, channel_id = (int(part) for part in parts)
        except ValueError:
            await message.answer("telegram_id и channel_id должны быть числами.")
            return
        removed = await AdminPermissionService(session).revoke_channel(telegram_id, channel_id)
        await _audit(
            session,
            message,
            "admin.channel.revoke",
            "admin_channel_access",
            None,
            {"telegram_id": telegram_id, "channel_id": channel_id, "found": removed},
        )
        await session.commit()
        await message.answer("Доступ отозван." if removed else "Такой доступ не найден.")


@router.message(Command("admin_admin_access"))
async def admin_admin_access(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, superadmin=True):
            return
        try:
            telegram_id = int(_command_payload(message))
        except ValueError:
            await message.answer("Формат: /admin_admin_access telegram_id")
            return
        admin = await session.scalar(
            select(Admin)
            .options(
                selectinload(Admin.channel_access).selectinload(AdminChannelAccess.channel)
            )
            .where(Admin.telegram_id == telegram_id)
        )
        if admin is None:
            await message.answer("Администратор не найден.")
            return
        lines = [
            f"Администратор {telegram_id}: {admin.role}",
            f"Область: {'все каналы' if admin.all_channels else 'только выбранные каналы'}",
        ]
        lines.extend(
            f"- #{access.channel_id} {access.channel.title}"
            for access in admin.channel_access
        )
        await message.answer("\n".join(lines))


@router.message(Command("admin_extend"))
async def admin_extend(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, manage=True):
            return
        parts = _pipe_payload(message)
        if len(parts) != 2:
            await message.answer("Формат: /admin_extend subscription_id | days")
            return
        try:
            subscription_id, days = (int(part) for part in parts)
            target = await session.get(Subscription, subscription_id)
            if target is None:
                raise ValueError("Subscription not found")
            if not await _authorize_channel(message, session, target.channel_id):
                return
            subscription, invite = await SubscriptionService(session).extend_manual(
                subscription_id,
                days,
                message.bot,
            )
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await _audit(
            session,
            message,
            "subscription.extend",
            "subscription",
            subscription.id,
            {"days": days},
        )
        await session.commit()
        await message.answer(
            access_delivery_text(
                expires_at=subscription.expires_at,
                primary_link=invite.link,
                materials_link=invite.materials_link,
            )
        )


@router.message(Command("admin_resend_invite"))
async def admin_resend_invite(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, manage=True):
            return
        try:
            subscription_id = int(_command_payload(message))
            target = await session.get(Subscription, subscription_id)
            if target is None:
                raise ValueError("Subscription not found")
            if not await _authorize_channel(message, session, target.channel_id):
                return
            subscription, invite = await SubscriptionService(session).refresh_invite(
                subscription_id,
                message.bot,
            )
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await _audit(session, message, "invite.refresh", "subscription", subscription.id)
        await session.commit()
        await message.answer(
            access_delivery_text(
                expires_at=subscription.expires_at,
                primary_link=invite.link,
                materials_link=invite.materials_link,
            )
        )


@router.message(Command("admin_check_channel"))
async def admin_check_channel(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session):
            return
        try:
            channel_id = int(_command_payload(message))
        except ValueError:
            await message.answer("Формат: /admin_check_channel channel_id")
            return
        channel = await session.get(Channel, channel_id)
        if channel is None:
            await message.answer("Канал не найден.")
            return
        if not await _authorize_channel(message, session, channel.id):
            return
        ok = await _bot_is_channel_admin(message, channel.telegram_chat_id)
        await message.answer(
            "Бот является администратором канала."
            if ok
            else "Бот не администратор или канал недоступен."
        )


@router.message(Command("admin_audit"))
async def admin_audit(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, superadmin=True):
            return
        logs = list(
            await session.scalars(
                select(AuditLog).order_by(AuditLog.id.desc()).limit(_limit(message))
            )
        )
        lines = ["Последние действия:"]
        lines.extend(
            f"#{log.id} tg:{log.actor_telegram_id or 'system'} {log.action} "
            f"{log.target_type}:{log.target_id or '—'}"
            for log in logs
        )
        await message.answer("\n".join(lines) if logs else "Журнал действий пуст.")


@router.message(Command("admin_notifications"))
async def admin_notifications(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, superadmin=True):
            return
        logs = list(
            await session.scalars(
                select(NotificationLog)
                .order_by(NotificationLog.id.desc())
                .limit(_limit(message))
            )
        )
        lines = ["Последние уведомления:"]
        lines.extend(
            f"#{log.id} user:{log.user_id} sub:{log.subscription_id} {log.type}"
            for log in logs
        )
        await message.answer("\n".join(lines) if logs else "Уведомлений пока нет.")


@router.message(Command("admin_add_admin"))
async def admin_add_admin(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, superadmin=True):
            return
        parts = _command_payload(message).split()
        try:
            telegram_id = int(parts[0])
            role = parts[1].lower() if len(parts) > 1 else "admin"
            admin = await AdminPermissionService(session).add_admin(telegram_id, role)
        except (ValueError, IndexError) as exc:
            await message.answer(f"Формат: /admin_add_admin telegram_id [admin|support]. {exc}")
            return
        await _audit(session, message, "admin.add", "admin", admin.id, {"role": role})
        await session.commit()
        await message.answer(f"Администратор {telegram_id} добавлен с ролью {role}.")


@router.message(Command("admin_remove_admin"))
async def admin_remove_admin(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, superadmin=True):
            return
        try:
            telegram_id = int(_command_payload(message))
        except ValueError:
            await message.answer("Формат: /admin_remove_admin telegram_id")
            return
        if telegram_id in get_settings().admin_ids:
            await message.answer("Superadmin из ADMIN_IDS нельзя удалить через бота.")
            return
        removed = await AdminPermissionService(session).remove_admin(telegram_id)
        await _audit(
            session,
            message,
            "admin.remove",
            "admin",
            telegram_id,
            {"found": removed},
        )
        await session.commit()
        await message.answer("Администратор отключён." if removed else "Администратор не найден.")


@router.message(Command("admin_debug_payment"))
async def admin_debug_payment(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session):
            return
        try:
            payment_id = int(_command_payload(message))
        except ValueError:
            await message.answer("Формат: /admin_debug_payment payment_id")
            return
        payment = await session.get(
            Payment,
            payment_id,
            options=[selectinload(Payment.plan)],
        )
        if payment is None:
            await message.answer("Платёж не найден.")
            return
        scope = await AdminPermissionService(session).channel_scope(message.from_user.id)
        if scope is not None and payment.plan.channel_id not in scope:
            await message.answer("Платёж недоступен.")
            return
        await message.answer(
            "\n".join(
                [
                    f"Payment #{payment.id}",
                    f"provider={payment.provider}",
                    f"provider_id={payment.provider_payment_id or '—'}",
                    f"status={payment.status.value}",
                    f"amount={payment.amount} {payment.currency}",
                    f"paid_at={_format_dt(payment.paid_at)}",
                ]
            )
        )


@router.message(Command("admin_debug_user"))
async def admin_debug_user(message: Message) -> None:
    await admin_user(message)


@router.message(Command("admin_debug_webhooks"))
async def admin_debug_webhooks(message: Message) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, superadmin=True):
            return
        events = list(
            await session.scalars(
                select(WebhookEvent).order_by(WebhookEvent.id.desc()).limit(_limit(message))
            )
        )
        lines = ["Последние webhook-события:"]
        lines.extend(
            f"#{event.id} {event.provider}/{event.event_type}: "
            f"{'ok' if event.processed_at else event.error or 'pending'}"
            for event in events
        )
        await message.answer("\n".join(lines) if events else "Webhook-событий пока нет.")


async def _set_channel_active(message: Message, active: bool) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, manage=True):
            return
        try:
            channel_id = int(_command_payload(message))
        except ValueError:
            await message.answer("Формат: /admin_enable_channel channel_id")
            return
        channel = await session.get(Channel, channel_id)
        if channel is None:
            await message.answer("Канал не найден.")
            return
        if not await _authorize_channel(message, session, channel.id):
            return
        channel.is_active = active
        await _audit(session, message, "channel.enable" if active else "channel.disable", "channel", channel.id)
        await session.commit()
        await message.answer("Канал включён." if active else "Канал отключён.")


async def _set_plan_active(message: Message, active: bool) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, manage=True):
            return
        try:
            plan_id = int(_command_payload(message))
        except ValueError:
            await message.answer("Формат: /admin_enable_plan plan_id")
            return
        plan = await session.get(Plan, plan_id)
        if plan is None:
            await message.answer("Тариф не найден.")
            return
        if not await _authorize_channel(message, session, plan.channel_id):
            return
        plan.is_active = active
        await _audit(session, message, "plan.enable" if active else "plan.disable", "plan", plan.id)
        await session.commit()
        await message.answer("Тариф включён." if active else "Тариф отключён.")


async def _authorize_message(
    message: Message,
    session: AsyncSession,
    manage: bool = False,
    superadmin: bool = False,
) -> bool:
    if message.from_user is None:
        return False
    service = AdminPermissionService(session)
    allowed = (
        service.is_superadmin(message.from_user.id)
        if superadmin
        else await service.can_manage(message.from_user.id)
        if manage
        else await service.is_admin(message.from_user.id)
    )
    if not allowed:
        await message.answer("Недостаточно прав.")
    return allowed


async def _authorize_callback(callback: CallbackQuery) -> bool:
    async with async_session_factory() as session:
        allowed = await AdminPermissionService(session).is_admin(callback.from_user.id)
    if not allowed:
        await callback.answer("Недостаточно прав.", show_alert=True)
    return allowed


async def _authorize_channel(
    message: Message,
    session: AsyncSession,
    channel_id: int,
) -> bool:
    if message.from_user is None:
        return False
    allowed = await AdminPermissionService(session).can_manage_channel(
        message.from_user.id,
        channel_id,
    )
    if not allowed:
        await message.answer("У тебя нет прав на управление этим каналом.")
    return allowed


async def _set_integer_setting(
    message: Message,
    key: str,
    minimum: int,
    maximum: int,
    unit: str,
) -> None:
    async with async_session_factory() as session:
        if not await _authorize_message(message, session, superadmin=True):
            return
        try:
            value = int(_command_payload(message))
            if not minimum <= value <= maximum:
                raise ValueError
        except ValueError:
            await message.answer(f"Укажи число от {minimum} до {maximum}.")
            return
        await RuntimeSettingsService(session).set(key, value, message.from_user.id)
        await _audit(session, message, f"settings.{key}", "setting", None, {"value": value})
        await session.commit()
        await message.answer(f"Настройка сохранена: {value} {unit}")


async def _bot_is_channel_admin(message: Message, chat_id: int) -> bool:
    try:
        bot_user = await message.bot.get_me()
        member = await message.bot.get_chat_member(chat_id, bot_user.id)
    except TelegramAPIError:
        return False
    return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}


async def _section_text(section: str, actor_telegram_id: int) -> str:
    async with async_session_factory() as session:
        if section == "channels":
            return await _channels_text(session, actor_telegram_id)
        if section == "plans":
            return await _plans_text(session, actor_telegram_id)
        if section == "payments":
            return await _payments_text(session, 10, actor_telegram_id)
        if section == "subscriptions":
            return await _subscriptions_text(session, 10, actor_telegram_id)
        if section == "stats":
            return await _stats_text(session, actor_telegram_id)
        if section == "admins":
            if not AdminPermissionService(session).is_superadmin(actor_telegram_id):
                return "Недостаточно прав."
            return await _admins_text(session)
    return "Раздел не найден."


async def _channels_text(session: AsyncSession, actor_telegram_id: int) -> str:
    scope = await AdminPermissionService(session).channel_scope(actor_telegram_id)
    query = select(Channel).order_by(Channel.id)
    if scope is not None:
        query = query.where(Channel.id.in_(scope))
    channels = list(await session.scalars(query))
    if not channels:
        return "Каналов пока нет."
    lines = ["Каналы:"]
    lines.extend(
        f"#{channel.id} {channel.title} ({channel.telegram_chat_id}) — "
        f"{'активен' if channel.is_active else 'отключён'}"
        for channel in channels
    )
    return "\n".join(lines)


async def _plans_text(session: AsyncSession, actor_telegram_id: int) -> str:
    scope = await AdminPermissionService(session).channel_scope(actor_telegram_id)
    query = select(Plan).options(selectinload(Plan.channel)).order_by(
        Plan.channel_id, Plan.price
    )
    if scope is not None:
        query = query.where(Plan.channel_id.in_(scope))
    plans = list(
        await session.scalars(query)
    )
    if not plans:
        return "Тарифов пока нет."
    lines = ["Тарифы:"]
    lines.extend(
        f"#{plan.id} {plan.channel.title}: {plan.title}, {plan.duration_days} дн., "
        f"{plan.price} {plan.currency} — {'активен' if plan.is_active else 'отключён'}"
        for plan in plans
    )
    return "\n".join(lines)


async def _payments_text(
    session: AsyncSession,
    limit: int,
    actor_telegram_id: int,
) -> str:
    scope = await AdminPermissionService(session).channel_scope(actor_telegram_id)
    query = (
        select(Payment)
        .options(selectinload(Payment.user), selectinload(Payment.plan))
        .order_by(Payment.id.desc())
        .limit(limit)
    )
    if scope is not None:
        query = query.join(Payment.plan).where(Plan.channel_id.in_(scope))
    payments = list(
        await session.scalars(query)
    )
    if not payments:
        return "Платежей пока нет."
    lines = ["Последние платежи:"]
    lines.extend(
        f"#{payment.id} tg:{payment.user.telegram_id} {payment.amount} {payment.currency} "
        f"{payment.provider}/{payment.status.value}"
        for payment in payments
    )
    return "\n".join(lines)


async def _subscriptions_text(
    session: AsyncSession,
    limit: int,
    actor_telegram_id: int,
) -> str:
    scope = await AdminPermissionService(session).channel_scope(actor_telegram_id)
    query = (
        select(Subscription)
        .options(selectinload(Subscription.user), selectinload(Subscription.channel))
        .order_by(Subscription.id.desc())
        .limit(limit)
    )
    if scope is not None:
        query = query.where(Subscription.channel_id.in_(scope))
    subscriptions = list(
        await session.scalars(query)
    )
    if not subscriptions:
        return "Подписок пока нет."
    lines = ["Последние подписки:"]
    lines.extend(
        f"#{subscription.id} tg:{subscription.user.telegram_id} {subscription.channel.title} "
        f"{subscription.status.value}, до {_format_dt(subscription.expires_at)}"
        for subscription in subscriptions
    )
    return "\n".join(lines)


async def _subscriptions_view(
    actor_telegram_id: int,
    limit: int = 20,
) -> tuple[str, InlineKeyboardMarkup]:
    async with async_session_factory() as session:
        scope = await AdminPermissionService(session).channel_scope(actor_telegram_id)
        query = (
            select(Subscription)
            .options(selectinload(Subscription.user), selectinload(Subscription.channel))
            .order_by(Subscription.id.desc())
            .limit(limit)
        )
        if scope is not None:
            query = query.where(Subscription.channel_id.in_(scope))
        subscriptions = list(
            await session.scalars(query)
        )
    rows: list[list[InlineKeyboardButton]] = []
    for subscription in subscriptions:
        user = subscription.user
        identity = f"@{user.username}" if user.username else f"ID {user.telegram_id}"
        label = f"{identity} | {subscription.channel.title} | {subscription.status.value}"
        profile_url = (
            f"https://t.me/{user.username.lstrip('@')}"
            if user.username
            else f"tg://user?id={user.telegram_id}"
        )
        rows.append([InlineKeyboardButton(text=label[:64], url=profile_url)])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="admin:menu")])
    text = "Подписки\n\nНажмите на пользователя, чтобы открыть его профиль."
    if not subscriptions:
        text = "Подписок пока нет."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _stats_text(session: AsyncSession, actor_telegram_id: int) -> str:
    scope = await AdminPermissionService(session).channel_scope(actor_telegram_id)
    users_query = select(func.count(User.id))
    active_query = select(func.count(Subscription.id)).where(
        Subscription.status == SubscriptionStatus.ACTIVE
    )
    paid_query = select(func.count(Payment.id)).where(Payment.status == PaymentStatus.PAID)
    revenue_query = (
        select(Payment.currency, func.sum(Payment.amount))
        .where(Payment.status == PaymentStatus.PAID)
        .group_by(Payment.currency)
    )
    if scope is not None:
        users_query = select(func.count(func.distinct(Subscription.user_id))).where(
            Subscription.channel_id.in_(scope)
        )
        active_query = active_query.where(Subscription.channel_id.in_(scope))
        paid_query = paid_query.join(Payment.plan).where(Plan.channel_id.in_(scope))
        revenue_query = revenue_query.join(Payment.plan).where(Plan.channel_id.in_(scope))
    users = await session.scalar(users_query) or 0
    active = await session.scalar(active_query) or 0
    paid = await session.scalar(paid_query) or 0
    revenue_rows = await session.execute(revenue_query)
    revenue = ", ".join(f"{amount} {currency}" for currency, amount in revenue_rows) or "0"
    return (
        "Статистика:\n"
        f"Пользователей: {users}\n"
        f"Активных подписок: {active}\n"
        f"Успешных платежей: {paid}\n"
        f"Выручка: {revenue}"
    )


async def _admins_text(session: AsyncSession) -> str:
    db_admins = list(await session.scalars(select(Admin).order_by(Admin.telegram_id)))
    lines = ["Superadmin из ADMIN_IDS:"]
    lines.extend(f"- {telegram_id}" for telegram_id in get_settings().admin_ids)
    lines.append("Администраторы из БД:")
    lines.extend(
        f"- {admin.telegram_id}: {admin.role}, {'active' if admin.is_active else 'disabled'}"
        for admin in db_admins
    )
    if not db_admins:
        lines.append("- нет")
    return "\n".join(lines)


async def _audit(
    session: AsyncSession,
    message: Message,
    action: str,
    target_type: str,
    target_id: int | str | None,
    metadata: dict | None = None,
) -> None:
    actor_id = message.from_user.id if message.from_user else None
    await AdminPermissionService(session).audit(
        actor_id,
        action,
        target_type,
        target_id,
        metadata,
    )


def _command_payload(message: Message) -> str:
    if message.text is None:
        return ""
    return message.text.partition(" ")[2].strip()


def _pipe_payload(message: Message) -> list[str]:
    return [part.strip() for part in _command_payload(message).split("|")]


def _limit(message: Message, default: int = 20) -> int:
    try:
        return max(1, min(100, int(_command_payload(message) or default)))
    except ValueError:
        return default


def _format_dt(value: object) -> str:
    return value.strftime("%d.%m.%Y %H:%M") if hasattr(value, "strftime") else "—"


def _revoke_result_text(changed: bool, error: str | None) -> str:
    if changed:
        return "Подписка отозвана, пользователь удалён из канала."
    if error:
        return f"Подписка отозвана в БД, Telegram не подтвердил удаление ({error}). Проверь права бота."
    return "Подписка отозвана."


async def _refund_payment(callback: CallbackQuery, payment: Payment) -> bool:
    if payment.provider == "dev":
        return True
    if not payment.provider_payment_id:
        return False
    if payment.provider == "telegram-stars":
        await callback.bot.refund_star_payment(
            user_id=payment.user.telegram_id,
            telegram_payment_charge_id=payment.provider_payment_id,
        )
        return True
    provider = get_payment_provider(payment.provider)
    return await provider.refund(payment.provider_payment_id, payment.amount)
