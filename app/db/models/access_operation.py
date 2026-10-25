from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.mixins import TimestampMixin


class AccessOperation(TimestampMixin, Base):
    __tablename__ = "access_operations"

    id: Mapped[int] = mapped_column(primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(255), unique=True)
    kind: Mapped[str] = mapped_column(String(32))
    payment_id: Mapped[int | None] = mapped_column(
        ForeignKey("payments.id", ondelete="CASCADE"), default=None
    )
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), default=None
    )
    chat_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    invite_link: Mapped[str | None] = mapped_column(String(512), default=None)
    expires_at_snapshot: Mapped[datetime | None] = mapped_column(default=None)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(index=True)
    completed_at: Mapped[datetime | None] = mapped_column(default=None, index=True)
    last_error: Mapped[str | None] = mapped_column(String(255), default=None)
