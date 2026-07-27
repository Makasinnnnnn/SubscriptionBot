from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.db.models.plan import Plan
    from app.db.models.promo_redemption import PromoRedemption


class PromoCode(TimestampMixin, Base):
    __tablename__ = "promo_codes"
    __table_args__ = (
        CheckConstraint("max_redemptions > 0", name="ck_promo_codes_max_redemptions"),
        CheckConstraint("redemptions_count >= 0", name="ck_promo_codes_redemptions_count"),
        CheckConstraint(
            "redemptions_count <= max_redemptions",
            name="ck_promo_codes_redemptions_within_limit",
        ),
        CheckConstraint("per_user_limit > 0", name="ck_promo_codes_per_user_limit"),
        CheckConstraint("valid_until > valid_from", name="ck_promo_codes_valid_window"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("plans.id", ondelete="RESTRICT"),
        index=True,
    )
    valid_from: Mapped[datetime] = mapped_column(index=True)
    valid_until: Mapped[datetime] = mapped_column(index=True)
    max_redemptions: Mapped[int] = mapped_column(Integer)
    redemptions_count: Mapped[int] = mapped_column(Integer, default=0)
    per_user_limit: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by_telegram_id: Mapped[int | None] = mapped_column(
        BigInteger,
        default=None,
        index=True,
    )

    plan: Mapped["Plan"] = relationship(back_populates="promo_codes")
    redemptions: Mapped[list["PromoRedemption"]] = relationship(
        back_populates="promo_code",
    )
