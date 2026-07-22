from datetime import datetime

from sqlalchemy import BigInteger, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.mixins import TimestampMixin


class AdminNotificationDelivery(TimestampMixin, Base):
    __tablename__ = "admin_notification_deliveries"
    __table_args__ = (
        UniqueConstraint("telegram_id", "deduplication_key", name="uq_admin_notification_delivery"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    deduplication_key: Mapped[str] = mapped_column(String(255), index=True)
    sent_at: Mapped[datetime]
