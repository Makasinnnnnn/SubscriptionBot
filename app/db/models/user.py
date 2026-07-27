from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.db.models.payment import Payment
    from app.db.models.promo_redemption import PromoRedemption
    from app.db.models.subscription import Subscription


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), default=None)
    first_name: Mapped[str | None] = mapped_column(String(255), default=None)
    last_name: Mapped[str | None] = mapped_column(String(255), default=None)

    payments: Mapped[list["Payment"]] = relationship(back_populates="user")
    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="user")
    promo_redemptions: Mapped[list["PromoRedemption"]] = relationship(
        back_populates="user",
    )
