from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.db.models.subscription import Subscription
    from app.db.models.user import User


class NotificationLog(TimestampMixin, Base):
    __tablename__ = "notification_logs"
    __table_args__ = (
        UniqueConstraint("subscription_id", "type", name="uq_notification_subscription_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
        index=True,
    )
    type: Mapped[str] = mapped_column(String(64), index=True)
    sent_at: Mapped[datetime]

    user: Mapped["User"] = relationship()
    subscription: Mapped["Subscription"] = relationship()
