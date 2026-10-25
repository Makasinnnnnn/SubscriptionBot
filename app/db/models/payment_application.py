from datetime import datetime

from sqlalchemy import Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PaymentApplication(Base):
    __tablename__ = "payment_applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(
        ForeignKey("payments.id", ondelete="RESTRICT"), unique=True
    )
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="RESTRICT"), index=True
    )
    days_granted: Mapped[int]
    applied_at: Mapped[datetime]
    starts_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    reversed_at: Mapped[datetime | None] = mapped_column(default=None)
    legacy: Mapped[bool] = mapped_column(Boolean, default=False)
