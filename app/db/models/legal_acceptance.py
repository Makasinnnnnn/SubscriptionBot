from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.db.models.user import User


class LegalAcceptance(TimestampMixin, Base):
    __tablename__ = "legal_acceptances"
    __table_args__ = (UniqueConstraint("user_id", "legal_version", name="uq_legal_user_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    legal_version: Mapped[str] = mapped_column(String(64), index=True)
    terms_version: Mapped[str] = mapped_column(String(64))
    privacy_version: Mapped[str] = mapped_column(String(64))
    refund_policy_version: Mapped[str] = mapped_column(String(64))
    accepted_at: Mapped[datetime]

    user: Mapped["User"] = relationship()
