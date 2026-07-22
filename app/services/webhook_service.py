import hashlib
import logging
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.enums import PaymentStatus
from app.db.models import Payment, Plan, User, WebhookEvent
from app.payments.registry import get_payment_provider
from app.services.payment_service import set_status_timestamp
from app.services.admin_notification_service import AdminNotificationService
from app.subscriptions.service import SubscriptionService

logger = logging.getLogger(__name__)


class WebhookService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def process(
        self,
        provider_name: str,
        payload: dict,
        headers: dict[str, str],
        raw_body: bytes,
    ) -> dict[str, str]:
        # Authenticate with the provider before persisting any attacker-controlled payload.
        provider = get_payment_provider(provider_name)
        result = await provider.handle_webhook(payload, headers, raw_body)
        event_id = self._event_id(provider_name, payload, raw_body)
        if await self._event_exists(event_id):
            return {"status": "duplicate"}

        event = WebhookEvent(
            provider=provider_name,
            event_id=event_id,
            event_type=str(payload.get("event", payload.get("update_type", "unknown"))),
            payload=result.raw_payload,
        )
        self.session.add(event)
        try:
            await self.session.flush()
        except IntegrityError:
            await self.session.rollback()
            return {"status": "duplicate"}

        if not result.provider_payment_id:
            event.error = "provider_payment_id_missing"
            await self.session.commit()
            return {"status": "rejected"}

        payment = await self.session.scalar(
            select(Payment)
            .where(
                Payment.provider == provider_name,
                Payment.provider_payment_id == result.provider_payment_id,
            )
            .with_for_update()
        )
        if payment is None:
            event.error = "payment_not_found"
            await self.session.commit()
            return {"status": "payment_not_found"}
        if provider_name == "cryptobot":
            invoice_payload = (payload.get("payload") or {}).get("payload")
            if invoice_payload is not None and str(invoice_payload) != str(payment.id):
                event.error = "invoice_payment_mismatch"
                await self.session.commit()
                return {"status": "rejected"}
        if payment.amount != result.amount or payment.currency.upper() != result.currency.upper():
            event.error = "amount_or_currency_mismatch"
            await self.session.commit()
            return {"status": "rejected"}

        if payment.status == PaymentStatus.PAID and result.status == PaymentStatus.PAID:
            event.processed_at = datetime.now(UTC).replace(tzinfo=None)
            await self.session.commit()
            return {"status": "duplicate"}
        if payment.status == PaymentStatus.REFUNDED:
            event.error = "invalid_status_transition"
            await self.session.commit()
            return {"status": "rejected"}
        if payment.status == PaymentStatus.PAID and result.status != PaymentStatus.PAID:
            event.error = "paid_status_cannot_be_downgraded"
            await self.session.commit()
            return {"status": "rejected"}

        payment.status = result.status
        payment.provider_payload = result.raw_payload
        set_status_timestamp(payment, result.status)
        if result.status == PaymentStatus.PAID:
            provision_failed = False
            try:
                await self._activate_and_notify(payment)
            except TelegramAPIError as exc:
                provision_failed = True
                event.error = "access_provision_failed"
                logger.warning(
                    "Paid payment access provisioning failed payment_id=%s error=%s",
                    payment.id,
                    type(exc).__name__,
                )
                await self._notify_user_access_pending(payment)
            plan = await self.session.get(Plan, payment.plan_id)
            if provision_failed:
                await self._notify_admins(
                    "errors",
                    "subscription.provision_failed",
                    f"Не удалось выдать доступ по оплаченному платежу #{payment.id}.",
                    str(payment.id),
                    plan.channel_id if plan else None,
                )
            await self._notify_admins(
                "payments",
                "payment.paid",
                f"Успешный платёж #{payment.id}: {payment.amount:g} {payment.currency}.",
                str(payment.id),
                plan.channel_id if plan else None,
            )
        elif result.status in {PaymentStatus.FAILED, PaymentStatus.EXPIRED}:
            plan = await self.session.get(Plan, payment.plan_id)
            await self._notify_admins(
                "payments",
                "payment.failed",
                f"Платёж #{payment.id} завершён со статусом {result.status.value}.",
                f"{payment.id}:{result.status.value}",
                plan.channel_id if plan else None,
            )

        event.processed_at = datetime.now(UTC).replace(tzinfo=None)
        await self.session.commit()
        logger.info(
            "Webhook processed provider=%s payment_id=%s status=%s",
            provider_name,
            payment.id,
            result.status.value,
        )
        return {"status": "ok"}

    async def _activate_and_notify(self, payment: Payment) -> None:
        bot = self._bot()
        try:
            subscription, invite, materials_invite = await SubscriptionService(self.session).activate_from_payment(
                payment,
                bot,
            )
            user = await self.session.get(User, payment.user_id)
            if bot is not None and user is not None:
                try:
                    await bot.send_message(
                        user.telegram_id,
                        (
                            "Оплата прошла успешно.\n\n"
                            f"Доступ активен до {subscription.expires_at.strftime('%d.%m.%Y %H:%M')}.\n"
                            f"Основной канал: {invite.link}"
                            + self._materials_delivery_suffix(materials_invite)
                        ),
                    )
                except TelegramAPIError as exc:
                    logger.warning(
                        "Payment notification failed payment_id=%s user_id=%s error=%s",
                        payment.id,
                        payment.user_id,
                        type(exc).__name__,
                    )
        finally:
            if bot is not None:
                await bot.session.close()

    async def _event_exists(self, event_id: str) -> bool:
        return (
            await self.session.scalar(
                select(WebhookEvent.id).where(WebhookEvent.event_id == event_id)
            )
            is not None
        )

    @staticmethod
    def _event_id(provider_name: str, payload: dict, raw_body: bytes) -> str:
        if provider_name == "cryptobot":
            invoice = payload.get("payload") or {}
            invoice_id = invoice.get("invoice_id")
            if invoice_id is not None:
                source_id = f"{payload.get('update_type', 'unknown')}:{invoice_id}:{invoice.get('status', 'unknown')}"
                return f"{provider_name}:{source_id}"
        if provider_name == "yookassa":
            obj = payload.get("object") or {}
            object_id = obj.get("id")
            if object_id is not None:
                source_id = f"{payload.get('event', 'unknown')}:{object_id}:{obj.get('status', 'unknown')}"
                return f"{provider_name}:{source_id}"
        source_id = payload.get("event_id") or payload.get("id")
        if source_id is None:
            source_id = hashlib.sha256(raw_body).hexdigest()
        return f"{provider_name}:{source_id}"

    @staticmethod
    def _bot() -> Bot | None:
        token = get_settings().bot_token
        return Bot(token=token) if token else None

    async def _notify_admins(
        self,
        category: str,
        event: str,
        text: str,
        key: str,
        channel_id: int | None = None,
    ) -> None:
        bot = self._bot()
        try:
            await AdminNotificationService(self.session, bot).notify(
                category, event, text, key, channel_id
            )
        finally:
            if bot is not None:
                await bot.session.close()

    async def _notify_user_access_pending(self, payment: Payment) -> None:
        bot = self._bot()
        if bot is None:
            return
        try:
            user = await self.session.get(User, payment.user_id)
            if user is not None:
                await bot.send_message(
                    user.telegram_id,
                    "Оплата подтверждена, но Telegram пока не создал ссылку доступа. "
                    "Поддержка уже уведомлена.",
                )
        except TelegramAPIError as exc:
            logger.warning(
                "Access pending notification failed payment_id=%s error=%s",
                payment.id,
                type(exc).__name__,
            )
        finally:
            await bot.session.close()

    @staticmethod
    def _materials_delivery_suffix(materials_invite: str | None) -> str:
        if materials_invite:
            return f"\nМетодички: {materials_invite}"
        if get_settings().materials_channel_id is not None:
            return "\nМетодички: ссылка временно недоступна, поддержка уже уведомлена."
        return ""
