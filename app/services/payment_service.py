from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.enums import PaymentStatus
from app.db.models import Payment, Plan, User
from app.payments.base import InvoiceRequest
from app.payments.registry import get_payment_provider
from app.services.legal_service import LegalService


class PaymentService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_payment(self, user: User, plan: Plan, provider_name: str) -> Payment:
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
        plan = authoritative_plan
        amount, currency = self._provider_price(plan, provider_name)
        # Serialize invoice creation per user so double-clicks cannot create two provider invoices.
        await self.session.scalar(select(User.id).where(User.id == user.id).with_for_update())
        now = datetime.now(UTC).replace(tzinfo=None)
        pending_cutoff = now - timedelta(hours=1)
        existing = await self.session.scalar(
            select(Payment)
            .where(
                Payment.user_id == user.id,
                Payment.plan_id == plan.id,
                Payment.provider == provider_name,
                Payment.status == PaymentStatus.PENDING,
                Payment.created_at >= pending_cutoff,
            )
            .order_by(Payment.created_at.desc())
        )
        if (
            existing is not None
            and existing.amount == amount
            and existing.currency.upper() == currency.upper()
            and existing.provider_payment_id
        ):
            return existing
        stale_pending = list(
            await self.session.scalars(
                select(Payment).where(
                    Payment.user_id == user.id,
                    Payment.plan_id == plan.id,
                    Payment.provider == provider_name,
                    Payment.status == PaymentStatus.PENDING,
                )
            )
        )
        for stale in stale_pending:
            stale.status = PaymentStatus.EXPIRED
            stale.failed_at = stale.failed_at or now
        idempotency_key = str(uuid4())
        payment = Payment(
            user_id=user.id,
            plan_id=plan.id,
            provider=provider_name,
            amount=amount,
            currency=currency,
            idempotency_key=idempotency_key,
            metadata_json={"plan_id": plan.id},
        )
        self.session.add(payment)
        await self.session.flush()

        try:
            invoice = await provider.create_invoice(
                InvoiceRequest(
                    payment_id=payment.id,
                    user_id=user.id,
                    plan_id=plan.id,
                    amount=amount,
                    currency=currency,
                    description=f"{plan.title} subscription",
                    idempotency_key=idempotency_key,
                    metadata={"payment_id": payment.id},
                )
            )
        except Exception:
            await self.session.rollback()
            raise

        payment.provider_payment_id = invoice.provider_payment_id
        payment.invoice_url = invoice.invoice_url
        payment.provider_payload = invoice.raw_payload
        await self.session.commit()
        return payment

    @staticmethod
    def _provider_price(plan: Plan, provider_name: str) -> tuple[Decimal, str]:
        if provider_name == "telegram-stars":
            if not plan.stars_price or plan.stars_price <= 0:
                raise ValueError("Stars price is not configured for this plan")
            return Decimal(plan.stars_price), "XTR"
        return Decimal(plan.price), plan.currency

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
            return payment
        payment.status = result.status
        payment.provider_payload = result.raw_payload
        set_status_timestamp(payment, result.status)
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
