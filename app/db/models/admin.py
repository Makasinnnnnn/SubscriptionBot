from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.db.models.admin_channel_access import AdminChannelAccess


class Admin(TimestampMixin, Base):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    role: Mapped[str] = mapped_column(String(32), default="admin")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    all_channels: Mapped[bool] = mapped_column(Boolean, default=True)

    channel_access: Mapped[list["AdminChannelAccess"]] = relationship(
        back_populates="admin",
        cascade="all, delete-orphan",
    )
