from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import PaymentStatus, SubscriptionStatus
from app.db.models import Payment, PaymentApplication, Refund, Subscription, User
from app.payments.registry import get_payment_provider


class RefundService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def request(self, payment: Payment, bot: Bot | None = None) -> bool:
        payment = await self.session.scalar(
            select(Payment)
            .where(Payment.id == payment.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if payment is None:
            raise ValueError("Payment not found")
        now = datetime.now(UTC).replace(tzinfo=None)
        application = await self.session.scalar(
            select(PaymentApplication).where(PaymentApplication.payment_id == payment.id)
        )
        if application is not None and application.legacy:
            raise ValueError("Historical access must be reconciled before refund")
        refund = await self.session.scalar(
            select(Refund).where(Refund.payment_id == payment.id).with_for_update()
        )
        if refund is not None and refund.state == "succeeded":
            return True
        if payment.status != PaymentStatus.PAID:
            raise ValueError("Only a confirmed payment can be refunded")
        if (
            refund is not None
            and refund.state == "creating"
            and refund.created_at > now - timedelta(minutes=2)
        ):
            raise ValueError("Refund request is in progress")
        if refund is not None and refund.state in {
            "canceled",
            "needs_manual_access_review",
            "needs_manual_provider_review",
        }:
            raise ValueError("Refund requires manual review")
        if refund is not None and payment.provider == "yookassa" and refund.external_id:
            result = await get_payment_provider(payment.provider).check_refund(refund.external_id)
            try:
                self._validate_provider_result(payment, result, refund.external_id)
            except ValueError:
                refund.state = "needs_manual_provider_review"
                await self.session.commit()
                raise
            refund.state = str(result.get("status", "unknown"))
            if refund.state == "succeeded":
                await self.apply_confirmed(payment, bot)
            await self.session.commit()
            return refund.state == "succeeded"
        if refund is not None and payment.provider not in {"dev", "yookassa"}:
            raise ValueError("Uncertain refund outcome must be reconciled before retry")
        if (
            refund is not None
            and payment.provider == "yookassa"
            and refund.created_at <= now - timedelta(hours=23)
        ):
            raise ValueError("Refund idempotence window expired; manual reconciliation required")
        if refund is None:
            refund = Refund(
                payment_id=payment.id,
                amount=payment.amount,
                currency=payment.currency,
                idempotency_key=str(uuid4()),
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
            self.session.add(refund)
        refund.state = "creating"
        await self.session.commit()
        try:
            if payment.provider == "dev":
                result = {"status": "succeeded"}
            elif payment.provider == "telegram-stars":
                user = await self.session.get(User, payment.user_id)
                if bot is None or user is None or not payment.provider_payment_id:
                    raise ValueError("Stars refund parameters are missing")
                await bot.refund_star_payment(
                    user_id=user.telegram_id, telegram_payment_charge_id=payment.provider_payment_id
                )
                result = {"status": "succeeded", "id": payment.provider_payment_id}
            elif payment.provider == "yookassa":
                result = await get_payment_provider(payment.provider).create_refund(
                    payment.provider_payment_id,
                    payment.amount,
                    refund.idempotency_key,
                    expected_currency=payment.currency,
                )
            else:
                raise ValueError("Provider does not support automatic refunds")
        except Exception:
            refund.state = "unknown"
            await self.session.commit()
            raise
        if payment.provider == "yookassa":
            try:
                self._validate_provider_result(payment, result)
            except ValueError:
                refund.state = "needs_manual_provider_review"
                await self.session.commit()
                raise
        refund.external_id = str(result["id"]) if result.get("id") else None
        refund.state = str(result.get("status", "unknown"))
        if refund.state == "succeeded":
            await self.apply_confirmed(payment, bot)
        await self.session.commit()
        return refund.state == "succeeded"

    @staticmethod
    def _validate_provider_result(
        payment: Payment, result: dict[str, Any], expected_id: str | None = None
    ) -> None:
        external_id = result.get("id")
        amount = result.get("amount")
        if not isinstance(external_id, str) or not external_id:
            raise ValueError("Provider refund identity is missing")
        if expected_id is not None and external_id != expected_id:
            raise ValueError("Provider refund identity mismatch")
        if result.get("payment_id") != payment.provider_payment_id:
            raise ValueError("Provider refund payment identity mismatch")
        amount = amount if isinstance(amount, dict) else {}
        try:
            value = Decimal(str(amount.get("value")))
        except InvalidOperation as exc:
            raise ValueError("Provider refund amount is invalid") from exc
        if not value.is_finite() or value != payment.amount:
            raise ValueError("Partial or mismatched refunds require manual reconciliation")
        if str(amount.get("currency", "")).upper() != payment.currency.upper():
            raise ValueError("Provider refund currency mismatch")
        if result.get("status") not in {"pending", "succeeded", "canceled"}:
            raise ValueError("Provider refund status is invalid")

    async def apply_confirmed(self, payment: Payment, bot: Bot | None = None) -> None:
        from app.subscriptions.service import SubscriptionService

        payment = await self.session.scalar(
            select(Payment)
            .where(Payment.id == payment.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if payment is None:
            raise ValueError("Payment not found")
        await self.session.scalar(
            select(User.id).where(User.id == payment.user_id).with_for_update()
        )
        now = datetime.now(UTC).replace(tzinfo=None)
        refund = await self.session.scalar(select(Refund).where(Refund.payment_id == payment.id))
        if refund is None:
            refund = Refund(
                payment_id=payment.id,
                amount=payment.amount,
                currency=payment.currency,
                idempotency_key=str(uuid4()),
                created_at=now,
            )
            self.session.add(refund)
        refund.state = "succeeded"
        refund.completed_at = refund.completed_at or now
        payment.status = PaymentStatus.REFUNDED
        payment.refunded_at = payment.refunded_at or now
        application = await self.session.scalar(
            select(PaymentApplication)
            .where(PaymentApplication.payment_id == payment.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if application is None or application.reversed_at is not None:
            return
        if application.legacy:
            refund.state = "needs_manual_access_review"
            return
        subscription = await self.session.scalar(
            select(Subscription)
            .where(Subscription.id == application.subscription_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if subscription is None:
            raise ValueError("Refunded access record not found")
        unused = max(
            application.expires_at - max(now, application.starts_at),
            application.expires_at - application.expires_at,
        )
        application.reversed_at = now
        if unused.total_seconds() > 0:
            later = await self.session.scalars(
                select(PaymentApplication).where(
                    PaymentApplication.subscription_id == subscription.id,
                    PaymentApplication.starts_at >= application.expires_at,
                    PaymentApplication.reversed_at.is_(None),
                )
            )
            for grant in later:
                grant.starts_at -= unused
                grant.expires_at -= unused
            subscription.expires_at = max(now, subscription.expires_at - unused)
        if subscription.expires_at <= now and subscription.status == SubscriptionStatus.ACTIVE:
            await SubscriptionService(self.session).revoke(
                subscription.id, bot, reason=f"payment {payment.id} refunded"
            )
