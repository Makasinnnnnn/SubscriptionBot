from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.db.models.admin import Admin
    from app.db.models.channel import Channel


class AdminChannelAccess(TimestampMixin, Base):
    __tablename__ = "admin_channel_access"
    __table_args__ = (
        UniqueConstraint("admin_id", "channel_id", name="uq_admin_channel_access"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int] = mapped_column(
        ForeignKey("admins.id", ondelete="CASCADE"),
        index=True,
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"),
        index=True,
    )
    can_manage: Mapped[bool] = mapped_column(Boolean, default=True)

    admin: Mapped["Admin"] = relationship(back_populates="channel_access")
    channel: Mapped["Channel"] = relationship()
