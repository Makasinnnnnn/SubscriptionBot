from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.db.models.payment import Payment
    from app.db.models.promo_code import PromoCode
    from app.db.models.subscription import Subscription
    from app.db.models.user import User


class PromoRedemption(TimestampMixin, Base):
    __tablename__ = "promo_redemptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    promo_code_id: Mapped[int] = mapped_column(
        ForeignKey("promo_codes.id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    payment_id: Mapped[int | None] = mapped_column(
        ForeignKey("payments.id", ondelete="SET NULL"),
        unique=True,
        default=None,
    )
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        index=True,
    )
    redeemed_at: Mapped[datetime] = mapped_column(index=True)

    promo_code: Mapped["PromoCode"] = relationship(back_populates="redemptions")
    payment: Mapped["Payment | None"] = relationship(
        back_populates="promo_redemption",
    )
    user: Mapped["User"] = relationship(back_populates="promo_redemptions")
    subscription: Mapped["Subscription"] = relationship(
        back_populates="promo_redemptions",
    )
