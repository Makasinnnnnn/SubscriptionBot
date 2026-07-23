import logging
from datetime import UTC, datetime
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    InlineKeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.bot.keyboards.subscription import (
    main_menu_keyboard,
    legal_acceptance_keyboard,
    payment_keyboard,
    payment_providers_keyboard,
    plans_keyboard,
)
from app.admin.settings_service import RuntimeSettingsService
from app.core.config import get_settings
from app.db.enums import PaymentStatus, SubscriptionStatus
from app.db.models import Channel, Payment, Plan, Subscription, User
from app.db.session import async_session_factory
from app.payments.providers import ProviderAPIError, ProviderNotConfiguredError
from app.services.payment_service import PaymentService
from app.services.legal_service import LegalAcceptanceRequiredError, LegalService
from app.services.admin_notification_service import AdminNotificationService
from app.subscriptions.service import SubscriptionService

router = Router()
logger = logging.getLogger(__name__)


async def _join_request_is_allowed(
    session: AsyncSession,
    *,
    telegram_id: int,
    chat_id: int,
    invite_value: str,
) -> bool:
    now = datetime.now(UTC).replace(tzinfo=None)
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        return False

    subscriptions = list(
        await session.scalars(
            select(Subscription)
            .options(
                selectinload(Subscription.channel),
                selectinload(Subscription.invite_link),
            )
            .where(
                Subscription.user_id == user.id,
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.expires_at > now,
            )
        )
    )
    primary_allowed = any(
        item.channel.telegram_chat_id == chat_id
        and item.invite_link is not None
        and item.invite_link.link == invite_value
        and not item.invite_link.is_revoked
        for item in subscriptions
    )
    if primary_allowed:
        return True

    materials_channel_id = await RuntimeSettingsService(session).get(
        RuntimeSettingsService.MATERIALS_CHANNEL_ID,
        get_settings().materials_channel_id,
    )
    return chat_id == materials_channel_id and any(
        item.invite_link is not None
        and item.invite_link.materials_channel_id == materials_channel_id
        and item.invite_link.materials_link == invite_value
        and not item.invite_link.materials_is_revoked
        for item in subscriptions
    )


@router.chat_join_request()
async def handle_paid_join_request(request: ChatJoinRequest) -> None:
    invite_value = request.invite_link.invite_link if request.invite_link else None
    if not invite_value:
        await request.bot.decline_chat_join_request(
            chat_id=request.chat.id,
            user_id=request.from_user.id,
        )
        return

    async with async_session_factory() as session:
        allowed = await _join_request_is_allowed(
            session,
            telegram_id=request.from_user.id,
            chat_id=request.chat.id,
            invite_value=invite_value,
        )

    if allowed:
        await request.bot.approve_chat_join_request(
            chat_id=request.chat.id,
            user_id=request.from_user.id,
        )
        return
    await request.bot.decline_chat_join_request(
        chat_id=request.chat.id,
        user_id=request.from_user.id,
    )


async def send_main_menu(message: Message) -> None:
    if message.from_user is None:
        return
    user = await _ensure_user(message)
    async with async_session_factory() as session:
        active_count = await session.scalar(
            select(Subscription.id)
            .where(
                Subscription.user_id == user.id,
                Subscription.status == SubscriptionStatus.ACTIVE,
            )
            .limit(1)
        )
    status = "У вас есть активная подписка." if active_count else "Активной подписки пока нет."
    await message.answer(
        f"PropGate\n\nОплата и управление доступом к закрытым каналам.\n{status}",
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(F.data == "menu:main")
async def main_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.edit_text("PropGate\n\nВыберите действие:", reply_markup=main_menu_keyboard())


@router.callback_query(F.data == "menu:help")
async def help_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    if not callback.message:
        return
    await callback.message.edit_text(
        "Помощь\n\n"
        "1. Выберите тариф.\n"
        "2. Оплатите счёт выбранным способом.\n"
        "3. После подтверждения оплаты бот пришлёт ссылку для входа.\n\n"
        "Команды: /support — поддержка, /terms — условия использования.",
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(F.data == "plans:list")
async def list_plans(callback: CallbackQuery) -> None:
    await callback.answer()
    async with async_session_factory() as session:
        plans = list(
            await session.scalars(
                select(Plan)
                .join(Plan.channel)
                .options(selectinload(Plan.channel))
                .where(Channel.is_active.is_(True), Plan.is_active.is_(True))
                .order_by(Channel.title, Plan.price)
            )
        )

    if not callback.message:
        return
    if not plans:
        await callback.message.edit_text(
            "Пока нет активных тарифов.",
            reply_markup=main_menu_keyboard(),
        )
        return
    await callback.message.edit_text(
        "Тарифы\n\nВыберите подходящий срок доступа:",
        reply_markup=plans_keyboard(plans),
    )


@router.callback_query(F.data.startswith("plan:"))
async def select_plan(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.data is None or callback.message is None:
        return
    try:
        plan_id = int(callback.data.split(":", maxsplit=1)[1])
    except ValueError:
        await callback.message.edit_text("Некорректный тариф.", reply_markup=main_menu_keyboard())
        return

    async with async_session_factory() as session:
        plan = await session.get(Plan, plan_id, options=[selectinload(Plan.channel)])

    if plan is None or not plan.is_active or not plan.channel.is_active:
        await callback.message.edit_text("Тариф недоступен.", reply_markup=main_menu_keyboard())
        return
    await callback.message.edit_text(
        f"{plan.channel.title}\n\n"
        f"{plan.title}\n"
        f"Доступ: {plan.duration_days} дн.\n"
        f"Стоимость: {plan.price:g} {plan.currency}\n\n"
        "Выберите способ оплаты:",
        reply_markup=payment_providers_keyboard(plan),
    )


@router.callback_query(F.data.startswith("pay:"))
async def create_payment(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.data is None or callback.message is None:
        return
    try:
        _, plan_id_raw, provider_name = callback.data.split(":", maxsplit=2)
        plan_id = int(plan_id_raw)
    except ValueError:
        await callback.message.edit_text("Некорректные параметры платежа.")
        return

    await _create_payment_flow(callback, plan_id, provider_name)


@router.callback_query(F.data == "payunavailable:sbp")
async def unavailable_sbp(callback: CallbackQuery) -> None:
    await callback.answer(
        "Оплата через СБП появится после подключения ЮKassa.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("legalaccept:"))
async def accept_legal_and_pay(callback: CallbackQuery) -> None:
    if callback.data is None or callback.message is None:
        return
    try:
        _, plan_id_raw, provider_name = callback.data.split(":", maxsplit=2)
        plan_id = int(plan_id_raw)
    except ValueError:
        await callback.answer("Некорректные параметры.", show_alert=True)
        return
    async with async_session_factory() as session:
        user = await _ensure_user_from_callback(callback, session)
        await LegalService(session).accept(user)
        await session.commit()
    await callback.answer("Условия приняты.")
    await _create_payment_flow(callback, plan_id, provider_name)


async def _create_payment_flow(
    callback: CallbackQuery,
    plan_id: int,
    provider_name: str,
) -> None:
    if callback.message is None:
        return
    async with async_session_factory() as session:
        user = await _ensure_user_from_callback(callback, session)
        plan = await session.get(Plan, plan_id, options=[selectinload(Plan.channel)])
        if plan is None or not plan.is_active or not plan.channel.is_active:
            await callback.message.edit_text("Тариф недоступен.", reply_markup=main_menu_keyboard())
            return
        try:
            payment = await PaymentService(session).create_payment(user, plan, provider_name)
        except LegalAcceptanceRequiredError:
            await session.rollback()
            await callback.message.edit_text(
                "Перед оплатой ознакомьтесь с документами и подтвердите согласие с их текущей версией.",
                reply_markup=legal_acceptance_keyboard(plan_id, provider_name),
            )
            return
        except (ProviderNotConfiguredError, ProviderAPIError, ValueError) as exc:
            await session.rollback()
            logger.warning(
                "Payment creation failed provider=%s plan_id=%s error=%s",
                provider_name,
                plan_id,
                type(exc).__name__,
            )
            await callback.message.edit_text(
                "Не удалось создать платёж выбранным способом. Попробуйте позже или обратитесь в поддержку.",
                reply_markup=payment_providers_keyboard(plan),
            )
            return

    if payment.provider == "telegram-stars":
        try:
            await callback.bot.send_invoice(
                chat_id=callback.from_user.id,
                title=plan.title[:32],
                description=f"Доступ к каналу {plan.channel.title}"[:255],
                payload=f"payment:{payment.id}",
                currency="XTR",
                prices=[LabeledPrice(label=plan.title[:32], amount=int(payment.amount))],
            )
        except TelegramAPIError:
            await callback.message.edit_text(
                "Telegram не смог создать счёт в Stars. Попробуй ещё раз позже.",
                reply_markup=payment_providers_keyboard(plan),
            )
            return
        await callback.message.edit_text(
            "Счёт в Telegram Stars отправлен отдельным сообщением.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await callback.message.edit_text(
        "Платёж создан. После успешной оплаты подписка активируется автоматически.",
        reply_markup=payment_keyboard(
            payment.invoice_url,
            payment.id,
            allow_dev_confirmation=payment.provider == "dev",
        ),
    )


@router.callback_query(F.data.startswith("devpay:"))
async def confirm_dev_payment(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.data is None or callback.message is None:
        return
    try:
        payment_id = int(callback.data.split(":", maxsplit=1)[1])
    except (IndexError, ValueError):
        await callback.answer("Некорректный платёж.", show_alert=True)
        return

    async with async_session_factory() as session:
        payment = await session.get(Payment, payment_id, options=[selectinload(Payment.user)])
        if payment is None or payment.user.telegram_id != callback.from_user.id:
            await callback.message.edit_text("Платёж не найден.", reply_markup=main_menu_keyboard())
            return
        if payment.provider != "dev" or not get_settings().enable_dev_payments:
            await callback.message.edit_text(
                "Этот платёж нельзя подтвердить вручную.",
                reply_markup=main_menu_keyboard(),
            )
            return
        if payment.status != PaymentStatus.PENDING:
            await callback.message.edit_text(
                "Тестовый платёж уже обработан или больше недоступен.",
                reply_markup=main_menu_keyboard(),
            )
            return

        payment.status = PaymentStatus.PAID
        subscription, invite, materials_invite = await SubscriptionService(session).activate_from_payment(
            payment,
            callback.bot,
        )
        await session.commit()
        await AdminNotificationService(session, callback.bot).notify(
            "payments",
            "payment.paid",
            f"Успешный тестовый платёж #{payment.id}: {payment.amount:g} {payment.currency}.",
            str(payment.id),
            subscription.channel_id,
        )
        await session.commit()

    await callback.message.edit_text(
        "Тестовая оплата подтверждена.\n\n"
        f"Доступ активен до {subscription.expires_at.strftime('%d.%m.%Y %H:%M')}.\n"
        f"Основной канал: {invite.link}"
        + _materials_delivery_suffix(materials_invite),
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(F.data == "subs:mine")
async def my_subscriptions(callback: CallbackQuery) -> None:
    await callback.answer()
    if not callback.message:
        return

    async with async_session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        if user is None:
            await callback.message.edit_text("Подписок пока нет.", reply_markup=main_menu_keyboard())
            return
        subscriptions = list(
            await session.scalars(
                select(Subscription)
                .options(selectinload(Subscription.channel))
                .where(
                    Subscription.user_id == user.id,
                    Subscription.status == SubscriptionStatus.ACTIVE,
                )
                .order_by(Subscription.expires_at.desc())
            )
        )

    if not subscriptions:
        await callback.message.edit_text(
            "Активных подписок пока нет.",
            reply_markup=main_menu_keyboard(),
        )
        return
    lines = ["Твои активные подписки:"]
    lines.extend(
        f"- {subscription.channel.title}: до "
        f"{subscription.expires_at.strftime('%d.%m.%Y %H:%M')}"
        for subscription in subscriptions
    )
    await callback.message.edit_text("\n".join(lines), reply_markup=_subscriptions_menu_keyboard())


def _subscriptions_menu_keyboard():
    keyboard = main_menu_keyboard()
    keyboard.inline_keyboard.insert(
        2,
        [InlineKeyboardButton(text="Получить доступ снова", callback_data="subs:renew")],
    )
    return keyboard


@router.callback_query(F.data == "subs:renew")
async def renew_access_links(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is None:
        return

    async with async_session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        if user is None:
            await callback.message.edit_text("Активных подписок пока нет.", reply_markup=main_menu_keyboard())
            return
        subscriptions = list(
            await session.scalars(
                select(Subscription)
                .options(selectinload(Subscription.channel), selectinload(Subscription.invite_link))
                .where(
                    Subscription.user_id == user.id,
                    Subscription.status == SubscriptionStatus.ACTIVE,
                )
                .order_by(Subscription.expires_at.desc())
            )
        )
        if not subscriptions:
            await callback.message.edit_text("Активных подписок пока нет.", reply_markup=main_menu_keyboard())
            return
        refreshed: list[tuple[Subscription, Any]] = []
        try:
            for subscription in subscriptions:
                _, invite = await SubscriptionService(session).refresh_invite(
                    subscription.id,
                    callback.bot,
                )
                refreshed.append((subscription, invite))
            await session.commit()
        except (TelegramAPIError, ValueError) as exc:
            await session.rollback()
            logger.warning("Access link refresh failed user_id=%s error=%s", user.id, type(exc).__name__)
            await callback.message.edit_text(
                "Не удалось создать новые ссылки. Поддержка уже уведомлена.",
                reply_markup=main_menu_keyboard(),
            )
            return

    lines = ["Новые ссылки для активных подписок:", ""]
    for subscription, invite in refreshed:
        lines.extend(
            [
                f"{subscription.channel.title} (до {subscription.expires_at:%d.%m.%Y %H:%M})",
                f"Основной канал: {invite.link}",
            ]
        )
        if invite.materials_link:
            lines.append(f"Методички: {invite.materials_link}")
        lines.append("")
    await callback.message.edit_text("\n".join(lines).strip(), reply_markup=_subscriptions_menu_keyboard())


@router.callback_query(F.data.startswith("payments:status:"))
async def payment_status(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.data is None or callback.message is None:
        return
    try:
        payment_id = int(callback.data.rsplit(":", maxsplit=1)[1])
    except ValueError:
        await callback.message.edit_text("Некорректный платёж.")
        return

    async with async_session_factory() as session:
        payment = await session.get(Payment, payment_id, options=[selectinload(Payment.user)])
        if payment is None or payment.user.telegram_id != callback.from_user.id:
            await callback.message.edit_text("Платёж не найден.", reply_markup=main_menu_keyboard())
            return
        if payment.provider == "dev":
            await callback.answer("Используй кнопку подтверждения тестовой оплаты.", show_alert=True)
            return
        try:
            payment = await PaymentService(session).refresh_status(payment)
        except (ProviderNotConfiguredError, ProviderAPIError, ValueError):
            await callback.message.edit_text(
                "Не удалось проверить платёж. Попробуй позже.",
                reply_markup=main_menu_keyboard(),
            )
            return
        if payment.status == PaymentStatus.PAID:
            try:
                subscription, invite, materials_invite = await SubscriptionService(
                    session
                ).activate_from_payment(payment, callback.bot)
            except (TelegramAPIError, ValueError) as exc:
                await session.rollback()
                logger.warning(
                    "Paid payment access provisioning failed payment_id=%s error=%s",
                    payment.id,
                    type(exc).__name__,
                )
                await callback.message.edit_text(
                    "Оплата подтверждена, но Telegram пока не выдал ссылку. "
                    "Поддержка уведомлена; повторите проверку позже.",
                    reply_markup=main_menu_keyboard(),
                )
                return
            await session.commit()
            await callback.message.edit_text(
                "Оплата подтверждена.\n\n"
                f"Доступ активен до {subscription.expires_at.strftime('%d.%m.%Y %H:%M')}.\n"
                f"Основной канал: {invite.link}"
                + _materials_delivery_suffix(materials_invite),
                reply_markup=main_menu_keyboard(),
            )
            return
        await callback.message.edit_text(
            f"Текущий статус платежа: {payment.status.value}.",
            reply_markup=main_menu_keyboard(),
        )


@router.pre_checkout_query()
async def telegram_stars_pre_checkout(query: PreCheckoutQuery) -> None:
    try:
        payment_id = _stars_payment_id(query.invoice_payload)
    except ValueError:
        await query.answer(ok=False, error_message="Счёт не найден.")
        return

    async with async_session_factory() as session:
        payment = await session.scalar(
            select(Payment)
            .options(
                selectinload(Payment.user),
                selectinload(Payment.plan).selectinload(Plan.channel),
            )
            .where(Payment.id == payment_id)
        )
        valid = (
            payment is not None
            and payment.provider == "telegram-stars"
            and payment.status == PaymentStatus.PENDING
            and payment.user.telegram_id == query.from_user.id
            and payment.currency.upper() == "XTR"
            and payment.amount == query.total_amount
            and payment.plan.is_active
            and payment.plan.channel.is_active
            and payment.plan.stars_price == query.total_amount
        )
    if not valid:
        await query.answer(ok=False, error_message="Параметры счёта изменились. Создай новый счёт.")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def telegram_stars_successful_payment(message: Message) -> None:
    successful = message.successful_payment
    if successful is None or message.from_user is None:
        return
    try:
        payment_id = _stars_payment_id(successful.invoice_payload)
    except ValueError:
        await message.answer("Платёж получен, но счёт не найден. Обратись в поддержку.")
        return

    async with async_session_factory() as session:
        payment = await session.scalar(
            select(Payment)
            .options(selectinload(Payment.user))
            .where(Payment.id == payment_id)
            .with_for_update()
        )
        if (
            payment is not None
            and payment.status == PaymentStatus.PAID
            and payment.provider_payment_id == successful.telegram_payment_charge_id
        ):
            await message.answer("Этот платёж уже обработан, доступ ранее был активирован.")
            return
        valid = (
            payment is not None
            and payment.provider == "telegram-stars"
            and payment.status == PaymentStatus.PENDING
            and payment.user.telegram_id == message.from_user.id
            and payment.currency.upper() == successful.currency.upper()
            and payment.amount == successful.total_amount
        )
        if not valid:
            await message.answer(
                "Параметры платежа не совпали. Доступ не выдан, обратись в поддержку."
            )
            return

        payment.provider_payment_id = successful.telegram_payment_charge_id
        payment.provider_payload = _stars_provider_payload(successful)
        payment.status = PaymentStatus.PAID
        try:
            subscription, invite, materials_invite = await SubscriptionService(
                session
            ).activate_from_payment(payment, message.bot)
        except TelegramAPIError as exc:
            await session.commit()
            logger.warning(
                "Stars access provisioning failed payment_id=%s error=%s",
                payment.id,
                type(exc).__name__,
            )
            await AdminNotificationService(session, message.bot).notify(
                "errors",
                "subscription.provision_failed",
                f"Не удалось выдать доступ по оплаченному платежу #{payment.id}.",
                str(payment.id),
            )
            await session.commit()
            await message.answer(
                "Оплата в Telegram Stars получена, но ссылка пока не создана. "
                "Поддержка уже уведомлена.",
                reply_markup=main_menu_keyboard(),
            )
            return
        await session.commit()
        await AdminNotificationService(session, message.bot).notify(
            "payments",
            "payment.paid",
            f"Успешный Stars-платёж #{payment.id}: {payment.amount:g} XTR.",
            str(payment.id),
            subscription.channel_id,
        )
        await session.commit()

    await message.answer(
        "Оплата в Telegram Stars прошла успешно.\n\n"
        f"Доступ активен до {subscription.expires_at.strftime('%d.%m.%Y %H:%M')}.\n"
        f"Основной канал: {invite.link}"
        + _materials_delivery_suffix(materials_invite),
        reply_markup=main_menu_keyboard(),
    )


async def _ensure_user(message: Message) -> User:
    async with async_session_factory() as session:
        user = await _upsert_user(
            session,
            message.from_user.id,
            message.from_user.username,
            message.from_user.first_name,
            message.from_user.last_name,
        )
        await session.commit()
        return user


async def _ensure_user_from_callback(callback: CallbackQuery, session: AsyncSession) -> User:
    return await _upsert_user(
        session,
        callback.from_user.id,
        callback.from_user.username,
        callback.from_user.first_name,
        callback.from_user.last_name,
    )


async def _upsert_user(
    session: AsyncSession,
    telegram_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    values: dict[str, Any] = {
        "username": username,
        "first_name": first_name,
        "last_name": last_name,
    }
    if user is None:
        user = User(telegram_id=telegram_id, **values)
        session.add(user)
    else:
        for field, value in values.items():
            setattr(user, field, value)
    await session.flush()
    return user


def _stars_payment_id(payload: str) -> int:
    prefix, separator, value = payload.partition(":")
    if prefix != "payment" or not separator:
        raise ValueError("Invalid Telegram Stars invoice payload")
    return int(value)


def _materials_delivery_suffix(materials_invite: str | None) -> str:
    if materials_invite:
        return f"\nМетодички: {materials_invite}"
    if get_settings().materials_channel_id is not None:
        return "\nМетодички: ссылка временно недоступна, поддержка уже уведомлена."
    return ""


def _stars_provider_payload(successful: Any) -> dict[str, object]:
    data = successful.model_dump(mode="json")
    allowed = (
        "currency",
        "total_amount",
        "invoice_payload",
        "telegram_payment_charge_id",
        "provider_payment_charge_id",
        "subscription_expiration_date",
        "is_recurring",
        "is_first_recurring",
    )
    return {key: data[key] for key in allowed if key in data}
