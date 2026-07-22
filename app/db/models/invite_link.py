from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.db.models.subscription import Subscription


class InviteLink(TimestampMixin, Base):
    __tablename__ = "invite_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id", ondelete="CASCADE"), unique=True)
    link: Mapped[str] = mapped_column(String(512))
    expire_at: Mapped[datetime | None] = mapped_column(default=None)
    used_at: Mapped[datetime | None] = mapped_column(default=None)
    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    provider_invite_link_id: Mapped[str | None] = mapped_column(String(255), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    materials_channel_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    materials_link: Mapped[str | None] = mapped_column(String(512), default=None)
    materials_expire_at: Mapped[datetime | None] = mapped_column(default=None)
    materials_is_revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    materials_revoked_at: Mapped[datetime | None] = mapped_column(default=None)

    subscription: Mapped["Subscription"] = relationship(back_populates="invite_link")
