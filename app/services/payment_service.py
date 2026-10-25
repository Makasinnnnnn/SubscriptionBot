from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.admin.settings_service import RuntimeSettingsService
from app.core.config import get_settings
from app.db.enums import PaymentStatus
from app.db.models import Payment, Plan, User
from app.payments.base import InvoiceRequest, PaymentProvider
from app.payments.money import normalized_amount
from app.payments.providers import ProviderAPIError
from app.payments.registry import get_payment_provider
from app.services.legal_service import LegalService
from app.services.promo_service import (
    PromoService,
    discounted_amount,
)


class PaymentService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_payment(
        self,
        user: User,
        plan: Plan,
        provider_name: str,
        *,
        promo_id: int | None = None,
    ) -> Payment:
        await LegalService(self.session).require(user.id)
        provider = get_payment_provider(provider_name)
        authoritative_plan = await self.session.scalar(
            select(Plan)
            .options(selectinload(Plan.channel))
            .where(Plan.id == plan.id)
            .with_for_update()
        )
        if (
            authoritative_plan is None
            or not authoritative_plan.is_active
            or not authoritative_plan.channel.is_active
        ):
            raise ValueError("Plan is not available")
        materials_channel_id = await RuntimeSettingsService(self.session).get(
            RuntimeSettingsService.MATERIALS_CHANNEL_ID,
            get_settings().materials_channel_id,
        )
        try:
            is_materials_plan = (
                materials_channel_id is not None
                and authoritative_plan.channel.telegram_chat_id == int(materials_channel_id)
            )
        except (TypeError, ValueError):
            is_materials_plan = False
        if is_materials_plan:
            raise ValueError("Service plan is not available for purchase")
        plan = authoritative_plan
        base_amount, currency = self._provider_price(plan, provider_name)
        stale_pending = list(
            await self.session.scalars(
                select(Payment)
                .where(
                    Payment.user_id == user.id,
                    Payment.plan_id == plan.id,
                    Payment.provider == provider_name,
                    Payment.status == PaymentStatus.PENDING,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        await self.session.scalar(select(User.id).where(User.id == user.id).with_for_update())
        now = datetime.now(UTC).replace(tzinfo=None)
        pending_cutoff = now - timedelta(hours=1)
        promo = None
        amount = base_amount
        if promo_id is not None:
            promo = await PromoService(self.session).load_for_payment(
                promo_id=promo_id,
                plan_id=plan.id,
                now=now,
            )
            amount = discounted_amount(
                base_amount,
                currency,
                promo.discount_percent,
            )
        existing = await self.session.scalar(
            select(Payment)
            .where(
                Payment.user_id == user.id,
                Payment.plan_id == plan.id,
                Payment.provider == provider_name,
                (
                    Payment.promo_code_id == promo.id
                    if promo is not None
                    else Payment.promo_code_id.is_(None)
                ),
                Payment.status == PaymentStatus.PENDING,
                or_(Payment.created_at >= pending_cutoff, Payment.provider_payment_id.is_(None))
                if promo is None
                else Payment.id > 0,
            )
            .order_by(Payment.created_at.desc())
        )
        if (
            existing is not None
            and existing.amount == amount
            and existing.currency.upper() == currency.upper()
            and existing.provider_payment_id
            and existing.purchased_duration_days == plan.duration_days
            and existing.purchased_channel_id == plan.channel_id
            and existing.metadata_json.get("terms_origin") != "migration_catalog_unverified"
        ):
            return existing
        if existing is not None and existing.amount == amount and existing.currency == currency:
            state = existing.metadata_json.get("invoice_state")
            if state == "creating":
                if existing.created_at > now - timedelta(minutes=2):
                    raise ValueError("Invoice creation is in progress; check again shortly")
                state = "unknown"
            if state == "unknown":
                if provider_name == "cryptobot":
                    invoice = await provider.reconcile_invoice(existing.id)
                    if invoice is None:
                        raise ProviderAPIError(
                            "Invoice outcome is unknown; provider reconciliation required"
                        )
                    recovered = await provider.check_payment_status(invoice.provider_payment_id)
                    if (
                        recovered.amount != existing.amount
                        or recovered.currency.upper() != existing.currency.upper()
                    ):
                        raise ProviderAPIError(
                            "Recovered invoice terms do not match the local attempt"
                        )
                    existing.provider_payment_id = invoice.provider_payment_id
                    existing.invoice_url = invoice.invoice_url
                    existing.provider_payload = invoice.raw_payload
                    existing.status = recovered.status
                    set_status_timestamp(existing, recovered.status)
                    if existing.status == PaymentStatus.PAID:
                        from app.services.access_operation_service import AccessOperationService

                        await AccessOperationService(self.session).enqueue_payment(existing.id)
                    existing.metadata_json = {**existing.metadata_json, "invoice_state": "created"}
                    await self.session.commit()
                    return existing
                if provider_name == "yookassa" and existing.created_at <= now - timedelta(hours=23):
                    raise ProviderAPIError(
                        "Invoice idempotence window expired; manual reconciliation required"
                    )
                existing.metadata_json = {**existing.metadata_json, "invoice_state": "creating"}
                await self.session.commit()
                return await self._create_invoice(
                    existing, provider, existing.metadata_json.get("plan_title", plan.title)
                )
        for stale in stale_pending:
            if stale.promo_code_id is not None or stale.metadata_json.get("invoice_state") in {
                "creating",
                "unknown",
            }:
                raise ValueError("Previous invoice must be reconciled before creating another")
            stale.status = PaymentStatus.EXPIRED
            stale.failed_at = stale.failed_at or now
        if promo is not None:
            await PromoService(self.session).ensure_capacity(
                promo,
                user.id,
                now,
            )
        idempotency_key = str(uuid4())
        payment = Payment(
            user_id=user.id,
            plan_id=plan.id,
            promo_code_id=promo.id if promo is not None else None,
            provider=provider_name,
            amount=amount,
            currency=currency,
            purchased_duration_days=plan.duration_days,
            purchased_channel_id=plan.channel_id,
            idempotency_key=idempotency_key,
            metadata_json={
                "plan_id": plan.id,
                "invoice_state": "creating",
                "plan_title": plan.title,
                "base_amount": format(base_amount.normalize(), "f"),
                "promo_code_id": promo.id if promo is not None else None,
                "promo_discount_percent": (promo.discount_percent if promo is not None else None),
            },
        )
        self.session.add(payment)
        await self.session.flush()
        await self.session.commit()
        return await self._create_invoice(payment, provider, plan.title)

    async def _create_invoice(
        self, payment: Payment, provider: PaymentProvider, title: str
    ) -> Payment:
        try:
            invoice = await provider.create_invoice(
                InvoiceRequest(
                    payment_id=payment.id,
                    user_id=payment.user_id,
                    plan_id=payment.plan_id,
                    amount=payment.amount,
                    currency=payment.currency,
                    description=f"{title} subscription",
                    idempotency_key=payment.idempotency_key,
                    metadata={
                        "payment_id": payment.id,
                        "promo_code_id": payment.promo_code_id,
                    },
                )
            )
        except Exception:
            payment.metadata_json = {**payment.metadata_json, "invoice_state": "unknown"}
            await self.session.commit()
            raise

        payment.provider_payment_id = invoice.provider_payment_id
        payment.invoice_url = invoice.invoice_url
        payment.provider_payload = invoice.raw_payload
        payment.metadata_json = {**payment.metadata_json, "invoice_state": "created"}
        await self.session.commit()
        return payment

    @staticmethod
    def _provider_price(plan: Plan, provider_name: str) -> tuple[Decimal, str]:
        if provider_name == "telegram-stars":
            if not plan.stars_price or plan.stars_price <= 0:
                raise ValueError("Stars price is not configured for this plan")
            return Decimal(plan.stars_price), "XTR"
        return normalized_amount(Decimal(plan.price), plan.currency), plan.currency.upper()

    async def refresh_status(self, payment: Payment) -> Payment:
        payment = await self.session.scalar(
            select(Payment).where(Payment.id == payment.id).with_for_update()
        )
        if payment is None:
            raise ValueError("Payment not found")
        if payment.status == PaymentStatus.REFUNDED:
            raise ValueError("Refunded payment status cannot be refreshed")
        if not payment.provider_payment_id:
            raise ValueError("Payment has no provider payment id")
        provider = get_payment_provider(payment.provider)
        result = await provider.check_payment_status(payment.provider_payment_id)
        if result.provider_payment_id != payment.provider_payment_id:
            raise ValueError("Provider payment id mismatch")
        if result.amount != payment.amount or result.currency.upper() != payment.currency.upper():
            raise ValueError("Provider amount or currency mismatch")
        if payment.status == PaymentStatus.PAID and result.status != PaymentStatus.PAID:
            from app.services.access_operation_service import AccessOperationService

            await AccessOperationService(self.session).enqueue_payment(payment.id)
            await self.session.commit()
            return payment
        payment.status = result.status
        payment.provider_payload = result.raw_payload
        set_status_timestamp(payment, result.status)
        if payment.status == PaymentStatus.PAID:
            from app.services.access_operation_service import AccessOperationService

            await AccessOperationService(self.session).enqueue_payment(payment.id)
        await self.session.commit()
        return payment


def set_status_timestamp(payment: Payment, status: PaymentStatus) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    if status == PaymentStatus.PAID:
        payment.paid_at = payment.paid_at or now
    elif status in {PaymentStatus.FAILED, PaymentStatus.EXPIRED}:
        payment.failed_at = payment.failed_at or now
    elif status == PaymentStatus.REFUNDED:
        payment.refunded_at = payment.refunded_at or now
