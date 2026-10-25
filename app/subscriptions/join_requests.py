from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.enums import SubscriptionStatus
from app.db.models import Subscription, User


@dataclass(frozen=True)
class JoinRequestDecision:
    allowed: bool
    reason: str
    subscription_id: int | None = None
    access_kind: str | None = None


class JoinRequestService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def decide(
        self,
        *,
        telegram_id: int,
        chat_id: int,
        invite_value: str,
        now: datetime | None = None,
    ) -> JoinRequestDecision:
        checked_at = now or datetime.now(UTC).replace(tzinfo=None)
        user = await self.session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            return JoinRequestDecision(False, "subscription_missing")

        subscriptions = list(
            await self.session.scalars(
                select(Subscription)
                .options(
                    selectinload(Subscription.channel),
                    selectinload(Subscription.invite_link),
                )
                .where(Subscription.user_id == user.id)
                .order_by(Subscription.expires_at.desc())
                .execution_options(populate_existing=True)
            )
        )
        if not subscriptions:
            return JoinRequestDecision(False, "subscription_missing")

        for subscription in subscriptions:
            invite = subscription.invite_link
            if invite is None:
                continue

            access_kind: str | None = None
            mode_enabled = False
            link_revoked = False
            link_expires_at: datetime | None = None
            if subscription.channel.telegram_chat_id == chat_id and invite.link == invite_value:
                access_kind = "primary"
                mode_enabled = invite.primary_join_request_enabled
                link_revoked = invite.is_revoked
                link_expires_at = invite.expire_at
            elif invite.materials_channel_id == chat_id and invite.materials_link == invite_value:
                access_kind = "materials"
                mode_enabled = invite.materials_join_request_enabled
                link_revoked = invite.materials_is_revoked
                link_expires_at = invite.materials_expire_at
            if access_kind is None:
                continue

            decision = self._validate_subscription(
                subscription,
                checked_at=checked_at,
                access_kind=access_kind,
                mode_enabled=mode_enabled,
                link_revoked=link_revoked,
                link_expires_at=link_expires_at,
            )
            return decision

        return JoinRequestDecision(False, "link_mismatch")

    @staticmethod
    def _validate_subscription(
        subscription: Subscription,
        *,
        checked_at: datetime,
        access_kind: str,
        mode_enabled: bool,
        link_revoked: bool,
        link_expires_at: datetime | None,
    ) -> JoinRequestDecision:
        details = {
            "subscription_id": subscription.id,
            "access_kind": access_kind,
        }
        if not mode_enabled:
            return JoinRequestDecision(False, "legacy_link", **details)
        if subscription.status == SubscriptionStatus.EXPIRED:
            return JoinRequestDecision(False, "subscription_expired", **details)
        if subscription.status != SubscriptionStatus.ACTIVE:
            return JoinRequestDecision(False, "subscription_cancelled", **details)
        if subscription.revoked_at is not None:
            return JoinRequestDecision(False, "subscription_revoked", **details)
        if subscription.expires_at <= checked_at:
            return JoinRequestDecision(False, "subscription_expired", **details)
        if link_revoked:
            return JoinRequestDecision(False, "link_revoked", **details)
        if link_expires_at is not None and link_expires_at <= checked_at:
            return JoinRequestDecision(False, "link_expired", **details)
        return JoinRequestDecision(True, "approved", **details)
