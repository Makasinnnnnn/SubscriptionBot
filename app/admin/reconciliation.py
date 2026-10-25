from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.service import AdminPermissionService
from app.db.enums import PaymentStatus
from app.db.models import AuditLog, Channel, Payment, PaymentApplication, Subscription, User
from app.services.access_operation_service import AccessOperationService


class PurchaseReconciliationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def reconcile(
        self,
        *,
        actor_id: int,
        payment_id: int,
        channel_id: int,
        duration_days: int,
        reference: str,
        apply: bool = False,
        confirmed_unapplied: bool = False,
    ) -> dict[str, Any]:
        if not AdminPermissionService(self.session).is_superadmin(actor_id):
            raise PermissionError("Сверка доступна только главному администратору.")
        if not 1 <= duration_days <= 3650 or not 1 <= len(reference.strip()) <= 255:
            raise ValueError("Укажите срок от 1 до 3650 дней и основание сверки до 255 символов.")
        payment = await self.session.scalar(
            select(Payment)
            .where(Payment.id == payment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if payment is None:
            raise ValueError("Платёж не найден.")
        await self.session.scalar(
            select(User.id).where(User.id == payment.user_id).with_for_update()
        )
        if payment.metadata_json.get("terms_origin") != "migration_catalog_unverified":
            raise ValueError("Условия этой покупки уже зафиксированы; сверка не меняет их.")
        if payment.status not in {PaymentStatus.PENDING, PaymentStatus.PAID, PaymentStatus.EXPIRED}:
            raise ValueError("Сверка не изменяет отменённые и возвращённые платежи.")
        application = await self.session.scalar(
            select(PaymentApplication.id).where(PaymentApplication.payment_id == payment.id)
        )
        subscription = await self.session.scalar(
            select(Subscription.id).where(Subscription.payment_id == payment.id)
        )
        if application is not None or subscription is not None:
            raise ValueError(
                "Оплата связана с доступом. Для неё нужны подтверждённые записи периодов; довыдача запрещена."
            )
        channel = await self.session.get(Channel, channel_id)
        if channel is None or not channel.is_active:
            raise ValueError("Выберите существующий активный канал.")
        changes = {
            "payment_id": payment.id,
            "status": payment.status.value,
            "provider": payment.provider,
            "amount": str(payment.amount),
            "currency": payment.currency,
            "before": {
                "channel_id": payment.purchased_channel_id,
                "duration_days": payment.purchased_duration_days,
            },
            "after": {"channel_id": channel_id, "duration_days": duration_days},
            "reference": reference.strip(),
            "applied": apply,
        }
        if not apply:
            return changes
        if not confirmed_unapplied:
            raise ValueError(
                "Перед применением подтвердите по журналу, что этот платёж ещё не выдавал доступ."
            )
        payment.purchased_channel_id = channel_id
        payment.purchased_duration_days = duration_days
        payment.metadata_json = {
            **payment.metadata_json,
            "terms_origin": "operator_receipt_verified",
            "terms_reference": reference.strip(),
        }
        self.session.add(
            AuditLog(
                actor_telegram_id=actor_id,
                action="payment.purchase_terms_reconciled",
                target_type="payment",
                target_id=str(payment.id),
                metadata_json=changes,
            )
        )
        if payment.status == PaymentStatus.PAID:
            operation = await AccessOperationService(self.session).enqueue_payment(payment.id)
            operation.completed_at = None
            operation.last_error = None
            operation.next_attempt_at = datetime.now(UTC).replace(tzinfo=None)
        await self.session.flush()
        return changes
