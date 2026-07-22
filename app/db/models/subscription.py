from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.enums import SubscriptionStatus, enum_values
from app.db.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.db.models.channel import Channel
    from app.db.models.invite_link import InviteLink
    from app.db.models.payment import Payment
    from app.db.models.user import User


class Subscription(TimestampMixin, Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    payment_id: Mapped[int | None] = mapped_column(
        ForeignKey("payments.id", ondelete="RESTRICT"),
        unique=True,
        default=None,
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, values_callable=enum_values),
        default=SubscriptionStatus.ACTIVE,
        index=True,
    )
    starts_at: Mapped[datetime]
    expires_at: Mapped[datetime] = mapped_column(index=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    revoke_reason: Mapped[str | None] = mapped_column(String(512), default=None)
    source: Mapped[str] = mapped_column(String(32), default="payment", index=True)

    user: Mapped["User"] = relationship(back_populates="subscriptions")
    channel: Mapped["Channel"] = relationship(back_populates="subscriptions")
    payment: Mapped["Payment | None"] = relationship(back_populates="subscription")
    invite_link: Mapped["InviteLink | None"] = relationship(back_populates="subscription")
