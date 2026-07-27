from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.db.models.promo_code import PromoCode
    from app.db.models.subscription import Subscription
    from app.db.models.user import User


class PromoRedemption(TimestampMixin, Base):
    __tablename__ = "promo_redemptions"
    __table_args__ = (
        UniqueConstraint(
            "promo_code_id",
            "user_id",
            name="uq_promo_redemptions_code_user",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    promo_code_id: Mapped[int] = mapped_column(
        ForeignKey("promo_codes.id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        index=True,
    )
    redeemed_at: Mapped[datetime] = mapped_column(index=True)

    promo_code: Mapped["PromoCode"] = relationship(back_populates="redemptions")
    user: Mapped["User"] = relationship(back_populates="promo_redemptions")
    subscription: Mapped["Subscription"] = relationship(
        back_populates="promo_redemptions",
    )
