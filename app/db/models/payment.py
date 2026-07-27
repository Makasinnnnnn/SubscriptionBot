from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Enum, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.enums import PaymentStatus, enum_values
from app.db.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.db.models.plan import Plan
    from app.db.models.promo_code import PromoCode
    from app.db.models.promo_redemption import PromoRedemption
    from app.db.models.subscription import Subscription
    from app.db.models.user import User


class Payment(TimestampMixin, Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_payment_id",
            name="uq_payments_provider_external_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id", ondelete="RESTRICT"), index=True)
    promo_code_id: Mapped[int | None] = mapped_column(
        ForeignKey("promo_codes.id", ondelete="SET NULL"),
        default=None,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(64), index=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(255), default=None)
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, values_callable=enum_values),
        default=PaymentStatus.PENDING,
        index=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(8))
    invoice_url: Mapped[str | None] = mapped_column(Text, default=None)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    metadata_json: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=dict)
    provider_payload: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        default=dict,
    )
    paid_at: Mapped[datetime | None] = mapped_column(default=None)
    failed_at: Mapped[datetime | None] = mapped_column(default=None)
    refunded_at: Mapped[datetime | None] = mapped_column(default=None)

    user: Mapped["User"] = relationship(back_populates="payments")
    plan: Mapped["Plan"] = relationship(back_populates="payments")
    promo_code: Mapped["PromoCode | None"] = relationship(back_populates="payments")
    promo_redemption: Mapped["PromoRedemption | None"] = relationship(
        back_populates="payment",
    )
    subscription: Mapped["Subscription | None"] = relationship(back_populates="payment")
