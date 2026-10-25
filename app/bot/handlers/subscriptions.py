import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    InlineKeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    Update,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.admin.settings_service import RuntimeSettingsService
from app.bot.keyboards.subscription import (
    legal_acceptance_keyboard,
    main_menu_keyboard,
    payment_keyboard,
    payment_providers_keyboard,
    plan_price_text,
    plans_keyboard,
)
from app.core.config import get_settings
from app.db.enums import PaymentStatus, SubscriptionStatus
from app.db.models import Channel, Payment, Plan, Subscription, User
from app.db.session import async_session_factory
from app.payments.providers import ProviderAPIError, ProviderNotConfiguredError
from app.services.access_operation_service import AccessOperationService
from app.services.admin_notification_service import AdminNotificationService
from app.services.legal_service import LegalAcceptanceRequiredError, LegalService
from app.services.payment_service import PaymentService, set_status_timestamp
from app.services.promo_service import PromoRedemptionError
from app.subscriptions.join_requests import JoinRequestService
from app.subscriptions.presentation import access_delivery_text
from app.subscriptions.service import SubscriptionService

router = Router()
logger = logging.getLogger(__name__)


@router.chat_join_request()
async def handle_paid_join_request(
    request: ChatJoinRequest, event_update: Update | None = None
) -> None:
    invite_value = request.invite_link.invite_link if request.invite_link else None
    logger.info(
        "event=join_request.received chat_id=%s telegram_id=%s has_invite=%s",
        request.chat.id,
        request.from_user.id,
        invite_value is not None,
    )
    if not invite_value:
        logger.warning(
            "event=join_request.no_invite chat_id=%s telegram_id=%s",
            request.chat.id,
            request.from_user.id,
        )
        await _decline_join_request(request)
        return

    async with async_session_factory() as session:
        await session.scalar(
            select(User.id).where(User.telegram_id == request.from_user.id).with_for_update()
        )
        decision = await JoinRequestService(session).decide(
            telegram_id=request.from_user.id,
            chat_id=request.chat.id,
            invite_value=invite_value,
        )
        if decision.allowed:
            operation = await AccessOperationService(session).enqueue_join(
                request.from_user.id,
                request.chat.id,
                invite_value,
                getattr(request, "date", datetime.now(UTC)),
                decision.subscription_id,
                update_id=event_update.update_id if event_update else None,
            )
            await session.commit()
            confirmed = await AccessOperationService(session).execute(operation, request.bot)
            await session.commit()

    if decision.allowed:
        if not confirmed or operation.last_error:
            await _report_join_request_api_failure(
                request, "approve", operation.last_error or "pending"
            )
            return
        logger.info(
            "event=join_request.approved chat_id=%s telegram_id=%s "
            "subscription_id=%s access_kind=%s",
            request.chat.id,
            request.from_user.id,
            decision.subscription_id,
            decision.access_kind,
        )
        return

    logger.warning(
        "event=join_request.declined chat_id=%s telegram_id=%s "
        "subscription_id=%s access_kind=%s reason=%s",
        request.chat.id,
        request.from_user.id,
        decision.subscription_id,
        decision.access_kind,
        decision.reason,
    )
    logger.warning(
        "event=join_request.%s chat_id=%s telegram_id=%s subscription_id=%s",
        decision.reason,
        request.chat.id,
        request.from_user.id,
        decision.subscription_id,
    )
    await _decline_join_request(request)


async def _decline_join_request(request: ChatJoinRequest) -> None:
    try:
        await request.bot.decline_chat_join_request(
            chat_id=request.chat.id,
            user_id=request.from_user.id,
        )
    except TelegramAPIError as exc:
        await _report_join_request_api_failure(request, "decline", exc)


async def _report_join_request_api_failure(
    request: ChatJoinRequest,
    action: str,
    exc: TelegramAPIError | str,
) -> None:
    logger.warning(
        "event=join_request.telegram_failed action=%s chat_id=%s telegram_id=%s error=%s",
        action,
        request.chat.id,
        request.from_user.id,
        exc if isinstance(exc, str) else type(exc).__name__,
    )
    async with async_session_factory() as session:
        await AdminNotificationService(session, request.bot).notify(
            "errors",
            "join_request.telegram_failed",
            f"Telegram не выполнил {action} для заявки в канал {request.chat.id}.",
            f"{action}:{request.chat.id}:{request.from_user.id}",
        )
        await session.commit()


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
async def main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "PropGate\n\nВыберите действие:", reply_markup=main_menu_keyboard()
        )


async def _materials_channel_id(session: AsyncSession) -> int | None:
    value = await RuntimeSettingsService(session).get(
        RuntimeSettingsService.MATERIALS_CHANNEL_ID,
        get_settings().materials_channel_id,
    )
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("Invalid materials channel id in runtime settings")
        return None


async def _available_plans(session: AsyncSession) -> list[Plan]:
    statement = (
        select(Plan)
        .join(Plan.channel)
        .options(selectinload(Plan.channel))
        .where(Channel.is_active.is_(True), Plan.is_active.is_(True))
        .order_by(Channel.title, Plan.price)
    )
    materials_channel_id = await _materials_channel_id(session)
    if materials_channel_id is not None:
        statement = statement.where(Channel.telegram_chat_id != materials_channel_id)
    return list(await session.scalars(statement))


async def _plan_is_available(
    session: AsyncSession,
    plan: Plan | None,
) -> bool:
    if plan is None or not plan.is_active or not plan.channel.is_active:
        return False
    materials_channel_id = await _materials_channel_id(session)
    return plan.channel.telegram_chat_id != materials_channel_id


@router.callback_query(F.data == "plans:list")
async def list_plans(callback: CallbackQuery) -> None:
    await callback.answer()
    async with async_session_factory() as session:
        plans = await _available_plans(session)

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
        if not await _plan_is_available(session, plan):
            await callback.message.edit_text(
                "Тариф недоступен.",
                reply_markup=main_menu_keyboard(),
            )
            return
    keyboard = payment_providers_keyboard(plan)
    available = any(
        (button.callback_data or "").startswith("pay:")
        for row in keyboard.inline_keyboard
        for button in row
    )
    await callback.message.edit_text(
        f"{plan.channel.title}\n\n"
        f"{plan.title}\n"
        f"Доступ: {plan.duration_days} дн.\n"
        f"Стоимость: {plan_price_text(plan)}\n\n"
        + (
            "Выберите способ оплаты:"
            if available
            else "Оплата этого тарифа временно недоступна. Откройте «Помощь», чтобы связаться с поддержкой."
        ),
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("pay:"))
async def create_payment(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.data is None or callback.message is None:
        return
    try:
        parts = callback.data.split(":")
        if len(parts) not in {3, 4}:
            raise ValueError
        _, plan_id_raw, provider_name = parts[:3]
        plan_id = int(plan_id_raw)
        promo_id = int(parts[3]) if len(parts) == 4 else None
    except ValueError:
        await callback.message.edit_text("Некорректные параметры платежа.")
        return

    await _create_payment_flow(callback, plan_id, provider_name, promo_id)


@router.callback_query(F.data.startswith("legalaccept:"))
async def accept_legal_and_pay(callback: CallbackQuery) -> None:
    if callback.data is None or callback.message is None:
        return
    try:
        parts = callback.data.split(":")
        if len(parts) not in {3, 4}:
            raise ValueError
        _, plan_id_raw, provider_name = parts[:3]
        plan_id = int(plan_id_raw)
        promo_id = int(parts[3]) if len(parts) == 4 else None
    except ValueError:
        await callback.answer("Некорректные параметры.", show_alert=True)
        return
    async with async_session_factory() as session:
        user = await _ensure_user_from_callback(callback, session)
        await LegalService(session).accept(user)
        await session.commit()
    await callback.answer("Условия приняты.")
    await _create_payment_flow(callback, plan_id, provider_name, promo_id)


async def _create_payment_flow(
    callback: CallbackQuery,
    plan_id: int,
    provider_name: str,
    promo_id: int | None = None,
) -> None:
    if callback.message is None:
        return
    async with async_session_factory() as session:
        user = await _ensure_user_from_callback(callback, session)
        plan = await session.get(Plan, plan_id, options=[selectinload(Plan.channel)])
        if not await _plan_is_available(session, plan):
            await callback.message.edit_text("Тариф недоступен.", reply_markup=main_menu_keyboard())
            return
        try:
            payment = await PaymentService(session).create_payment(
                user,
                plan,
                provider_name,
                promo_id=promo_id,
            )
        except LegalAcceptanceRequiredError:
            await session.rollback()
            await callback.message.edit_text(
                "Перед оплатой ознакомьтесь с документами и подтвердите согласие с их текущей версией.",
                reply_markup=legal_acceptance_keyboard(
                    plan_id,
                    provider_name,
                    promo_id,
                ),
            )
            return
        except PromoRedemptionError as exc:
            await session.rollback()
            await callback.message.edit_text(
                f"Промокод не применён: {exc}",
                reply_markup=main_menu_keyboard(),
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
                reply_markup=payment_providers_keyboard(plan, promo_id),
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
                reply_markup=payment_providers_keyboard(plan, promo_id),
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
        subscription, invite, materials_invite = await SubscriptionService(
            session
        ).activate_from_payment(
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
        + access_delivery_text(
            expires_at=subscription.expires_at,
            primary_link=invite.link,
            materials_link=materials_invite,
        ),
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
            await callback.message.edit_text(
                "Подписок пока нет.", reply_markup=main_menu_keyboard()
            )
            return
        subscriptions = list(
            await session.scalars(
                select(Subscription)
                .options(selectinload(Subscription.channel))
                .where(
                    Subscription.user_id == user.id,
                    Subscription.status == SubscriptionStatus.ACTIVE,
                    Subscription.expires_at > datetime.now(UTC).replace(tzinfo=None),
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
        f"- {subscription.channel.title}: до {subscription.expires_at.strftime('%d.%m.%Y %H:%M')}"
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
            await callback.message.edit_text(
                "Активных подписок пока нет.", reply_markup=main_menu_keyboard()
            )
            return
        subscriptions = list(
            await session.scalars(
                select(Subscription)
                .options(selectinload(Subscription.channel), selectinload(Subscription.invite_link))
                .where(
                    Subscription.user_id == user.id,
                    Subscription.status == SubscriptionStatus.ACTIVE,
                    Subscription.expires_at > datetime.now(UTC).replace(tzinfo=None),
                )
                .order_by(Subscription.expires_at.desc())
            )
        )
        if not subscriptions:
            await callback.message.edit_text(
                "Активных подписок пока нет.", reply_markup=main_menu_keyboard()
            )
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
            logger.warning(
                "Access link refresh failed telegram_id=%s error=%s detail=%s",
                callback.from_user.id,
                type(exc).__name__,
                str(exc)[:300],
            )
            await session.rollback()
            await callback.message.edit_text(
                "Не удалось создать новые ссылки. Поддержка уже уведомлена.",
                reply_markup=main_menu_keyboard(),
            )
            return

    lines = ["Новые ссылки для активных подписок:", ""]
    for subscription, invite in refreshed:
        lines.append(
            access_delivery_text(
                expires_at=subscription.expires_at,
                primary_link=invite.link,
                materials_link=invite.materials_link,
            )
        )
        lines.append("")
    await callback.message.edit_text(
        "\n".join(lines).strip(), reply_markup=_subscriptions_menu_keyboard()
    )


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
            await callback.answer(
                "Используй кнопку подтверждения тестовой оплаты.", show_alert=True
            )
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
            await AccessOperationService(session).enqueue_payment(payment_id)
            try:
                async with session.begin_nested():
                    subscription, invite, materials_invite = await SubscriptionService(
                        session
                    ).activate_from_payment(payment, callback.bot)
                await AccessOperationService(session).complete_payment(payment_id)
            except (TelegramAPIError, ValueError) as exc:
                await AccessOperationService(session).fail_payment(payment_id, exc)
                await AccessOperationService(session).enqueue_cleanup(exc)
                logger.warning(
                    "Paid payment access provisioning failed payment_id=%s error=%s detail=%s",
                    payment_id,
                    type(exc).__name__,
                    str(exc)[:300],
                )
                await session.commit()
                await callback.message.edit_text(
                    "Оплата подтверждена, но Telegram пока не выдал ссылку. "
                    "Повторная выдача запланирована автоматически.",
                    reply_markup=main_menu_keyboard(),
                )
                return
            await session.commit()
            await callback.message.edit_text(
                "Оплата подтверждена.\n\n"
                + access_delivery_text(
                    expires_at=subscription.expires_at,
                    primary_link=invite.link,
                    materials_link=materials_invite,
                ),
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
            and payment.created_at >= datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        )
        if valid:
            valid = await _plan_is_available(session, payment.plan)
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
            .options(
                selectinload(Payment.user),
                selectinload(Payment.subscription),
            )
            .where(Payment.id == payment_id)
            .with_for_update()
        )
        already_provisioned = (
            payment is not None
            and payment.status == PaymentStatus.PAID
            and payment.provider_payment_id == successful.telegram_payment_charge_id
            and payment.subscription is not None
        )
        if already_provisioned:
            await message.answer("Этот платёж уже обработан, доступ ранее был активирован.")
            return
        retry_paid = (
            payment is not None
            and payment.status == PaymentStatus.PAID
            and payment.provider_payment_id == successful.telegram_payment_charge_id
            and payment.subscription is None
        )
        valid = (
            payment is not None
            and payment.provider == "telegram-stars"
            and payment.user.telegram_id == message.from_user.id
            and payment.currency.upper() == successful.currency.upper()
            and payment.amount == successful.total_amount
            and (payment.status in {PaymentStatus.PENDING, PaymentStatus.EXPIRED} or retry_paid)
        )
        if not valid:
            await message.answer(
                "Параметры платежа не совпали. Доступ не выдан, обратись в поддержку."
            )
            return

        charge_owner = await session.scalar(
            select(Payment.id).where(
                Payment.provider == "telegram-stars",
                Payment.provider_payment_id == successful.telegram_payment_charge_id,
                Payment.id != payment_id,
            )
        )
        if charge_owner is not None:
            await message.answer(
                "Идентификатор оплаты уже связан с другим счётом. Обратись в поддержку."
            )
            return
        payment.provider_payment_id = successful.telegram_payment_charge_id
        payment.provider_payload = _stars_provider_payload(successful)
        payment.status = PaymentStatus.PAID
        set_status_timestamp(payment, PaymentStatus.PAID)
        await AccessOperationService(session).enqueue_payment(payment_id)
        try:
            async with session.begin_nested():
                subscription, invite, materials_invite = await SubscriptionService(
                    session
                ).activate_from_payment(payment, message.bot)
            await AccessOperationService(session).complete_payment(payment_id)
        except (TelegramAPIError, ValueError) as exc:
            await session.refresh(payment)
            await AccessOperationService(session).fail_payment(payment_id, exc)
            await AccessOperationService(session).enqueue_cleanup(exc)
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
        + access_delivery_text(
            expires_at=subscription.expires_at,
            primary_link=invite.link,
            materials_link=materials_invite,
        ),
        reply_markup=main_menu_keyboard(),
    )


@router.message(F.refunded_payment)
async def telegram_stars_refunded_payment(message: Message) -> None:
    from app.services.refund_service import RefundService

    refunded = message.refunded_payment
    if refunded is None or message.from_user is None:
        return
    try:
        payment_id = _stars_payment_id(refunded.invoice_payload)
    except ValueError:
        return
    async with async_session_factory() as session:
        payment = await session.scalar(
            select(Payment)
            .options(selectinload(Payment.user))
            .where(Payment.id == payment_id)
            .with_for_update()
        )
        if (
            payment is None
            or payment.provider != "telegram-stars"
            or payment.user.telegram_id != message.from_user.id
            or payment.provider_payment_id != refunded.telegram_payment_charge_id
            or payment.currency.upper() != refunded.currency.upper()
            or payment.amount != refunded.total_amount
        ):
            return
        await RefundService(session).apply_confirmed(payment, message.bot)
        await session.commit()


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
