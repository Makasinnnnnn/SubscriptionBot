import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.enums import PaymentStatus, SubscriptionStatus
from app.db.models import (
    AccessOperation,
    InviteLink,
    Payment,
    PaymentApplication,
    Subscription,
    User,
)


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class AccessOperationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue(self, key: str, kind: str, **values) -> AccessOperation:
        operation = await self.session.scalar(
            select(AccessOperation).where(AccessOperation.operation_key == key)
        )
        if operation is None:
            operation = AccessOperation(
                operation_key=key, kind=kind, next_attempt_at=utcnow(), **values
            )
            self.session.add(operation)
            await self.session.flush()
        return operation

    async def enqueue_payment(self, payment_id: int) -> AccessOperation:
        return await self.enqueue(f"payment:{payment_id}", "provision", payment_id=payment_id)

    async def enqueue_join(
        self,
        telegram_id: int,
        chat_id: int,
        link: str,
        requested_at: datetime,
        subscription_id: int,
        *,
        update_id: int | None = None,
    ) -> AccessOperation:
        event_key = str(update_id) if update_id is not None else requested_at.isoformat()
        key = hashlib.sha256(f"{telegram_id}:{chat_id}:{link}:{event_key}".encode()).hexdigest()
        return await self.enqueue(
            f"join:{key}",
            "approve_join",
            subscription_id=subscription_id,
            telegram_id=telegram_id,
            chat_id=chat_id,
            invite_link=link,
        )

    async def complete_payment(self, payment_id: int) -> None:
        operation = await self.session.scalar(
            select(AccessOperation).where(AccessOperation.operation_key == f"payment:{payment_id}")
        )
        if operation:
            operation.completed_at = utcnow()
            operation.last_error = None

    async def fail_payment(self, payment_id: int, exc: Exception) -> None:
        operation = await self.enqueue_payment(payment_id)
        if operation.completed_at is None:
            await self._record_failure(operation, exc)

    async def _record_failure(self, operation: AccessOperation, exc: Exception) -> None:
        operation.attempts += 1
        operation.last_error = type(exc).__name__
        cause: BaseException | None = exc
        delay = min(3600, 2 ** min(operation.attempts, 12))
        for _ in range(8):
            if isinstance(cause, TelegramRetryAfter):
                delay = max(1, cause.retry_after)
                break
            cause = cause.__cause__ if cause else None
        operation.next_attempt_at = utcnow() + timedelta(seconds=delay)
        await self.session.flush()

    async def enqueue_cleanup(self, exc: Exception) -> None:
        for chat_id, link in getattr(exc, "cleanup_invites", []):
            key = hashlib.sha256(f"{chat_id}:{link}".encode()).hexdigest()
            await self.enqueue(f"cleanup:{key}", "cleanup_link", chat_id=chat_id, invite_link=link)

    async def execute(self, operation: AccessOperation, bot: Bot | None) -> bool:
        if operation.completed_at is not None:
            return True
        if bot is None:
            return False
        try:
            async with self.session.begin_nested():
                if operation.kind == "provision":
                    from app.subscriptions.presentation import access_delivery_text
                    from app.subscriptions.service import SubscriptionService

                    payment = await self.session.scalar(
                        select(Payment)
                        .options(selectinload(Payment.user))
                        .where(Payment.id == operation.payment_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    if payment is None or payment.status != PaymentStatus.PAID:
                        operation.completed_at = utcnow()
                        return True
                    await self.session.scalar(
                        select(User.id).where(User.id == payment.user_id).with_for_update()
                    )
                    await self.session.refresh(operation)
                    if operation.completed_at is not None:
                        return True
                    application = await self.session.scalar(
                        select(PaymentApplication)
                        .where(PaymentApplication.payment_id == payment.id)
                        .with_for_update()
                    )
                    if application is not None:
                        entitlement = await self.session.scalar(
                            select(Subscription)
                            .where(Subscription.id == application.subscription_id)
                            .with_for_update()
                            .execution_options(populate_existing=True)
                        )
                        if (
                            entitlement is None
                            or entitlement.status != SubscriptionStatus.ACTIVE
                            or entitlement.expires_at <= utcnow()
                        ):
                            operation.completed_at = utcnow()
                            operation.last_error = "entitlement_inactive"
                            return True
                    subscription, invite, materials = await SubscriptionService(
                        self.session
                    ).activate_from_payment(payment, bot)
                    await bot.send_message(
                        payment.user.telegram_id,
                        access_delivery_text(
                            expires_at=subscription.expires_at,
                            primary_link=invite.link,
                            materials_link=materials,
                        ),
                    )
                elif operation.kind == "cleanup_link":
                    await bot.revoke_chat_invite_link(
                        chat_id=operation.chat_id, invite_link=operation.invite_link
                    )
                elif operation.kind == "approve_join":
                    from app.subscriptions.join_requests import JoinRequestService

                    await self.session.scalar(
                        select(User.id)
                        .where(User.telegram_id == operation.telegram_id)
                        .with_for_update()
                    )
                    await self.session.refresh(operation)
                    if operation.completed_at is not None:
                        return True
                    decision = await JoinRequestService(self.session).decide(
                        telegram_id=operation.telegram_id,
                        chat_id=operation.chat_id,
                        invite_value=operation.invite_link,
                    )
                    if not decision.allowed:
                        await bot.decline_chat_join_request(
                            chat_id=operation.chat_id, user_id=operation.telegram_id
                        )
                        operation.completed_at = utcnow()
                        operation.last_error = f"join_{decision.reason}"
                        return True
                    await bot.approve_chat_join_request(
                        chat_id=operation.chat_id, user_id=operation.telegram_id
                    )
                else:
                    await self.session.scalar(
                        select(User.id)
                        .where(User.telegram_id == operation.telegram_id)
                        .with_for_update()
                    )
                    await self.session.refresh(operation)
                    if operation.completed_at is not None:
                        return True
                    subscription = await self.session.scalar(
                        select(Subscription)
                        .options(
                            selectinload(Subscription.user),
                            selectinload(Subscription.channel),
                            selectinload(Subscription.invite_link),
                        )
                        .where(Subscription.id == operation.subscription_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    if (
                        subscription is None
                        or subscription.status == SubscriptionStatus.ACTIVE
                        or subscription.expires_at != operation.expires_at_snapshot
                    ):
                        operation.completed_at = utcnow()
                        return True
                    if operation.kind == "expiry_notice":
                        from app.subscriptions.service import SubscriptionService

                        await SubscriptionService(self.session)._notify_expired(subscription, bot)
                    elif operation.kind == "remove_member":
                        if await self._other_entitlement(operation, subscription):
                            operation.completed_at = utcnow()
                            return True
                        await bot.ban_chat_member(
                            chat_id=operation.chat_id, user_id=operation.telegram_id
                        )
                        await bot.unban_chat_member(
                            chat_id=operation.chat_id,
                            user_id=operation.telegram_id,
                            only_if_banned=True,
                        )
                    elif operation.kind == "revoke_link":
                        await bot.revoke_chat_invite_link(
                            chat_id=operation.chat_id, invite_link=operation.invite_link
                        )
                        invite = subscription.invite_link
                        if invite and invite.link == operation.invite_link:
                            invite.is_revoked = True
                            invite.revoked_at = utcnow()
                        if invite and invite.materials_link == operation.invite_link:
                            invite.materials_is_revoked = True
                            invite.materials_revoked_at = utcnow()
                    else:
                        raise ValueError("Unknown access operation")
                operation.completed_at = utcnow()
                operation.last_error = None
        except (TelegramAPIError, ValueError) as exc:
            await self.session.refresh(operation)
            if (
                operation.kind == "approve_join"
                and isinstance(exc, TelegramBadRequest)
                and any(
                    code in exc.message.upper()
                    for code in ("USER_ALREADY_PARTICIPANT", "HIDE_REQUESTER_MISSING")
                )
            ):
                verification = await self._acknowledge_join_error(operation, bot)
                if verification is True:
                    return True
                if isinstance(verification, TelegramAPIError):
                    exc = verification
            if (
                operation.kind in {"revoke_link", "cleanup_link"}
                and isinstance(exc, TelegramBadRequest)
                and (
                    "already revoked" in exc.message.lower() or "INVITE_HASH_EXPIRED" in exc.message
                )
            ):
                operation.completed_at = utcnow()
                operation.last_error = None
                if operation.subscription_id is not None:
                    invite = await self.session.scalar(
                        select(InviteLink).where(
                            InviteLink.subscription_id == operation.subscription_id
                        )
                    )
                    if invite and invite.link == operation.invite_link:
                        invite.is_revoked = True
                        invite.revoked_at = utcnow()
                    if invite and invite.materials_link == operation.invite_link:
                        invite.materials_is_revoked = True
                        invite.materials_revoked_at = utcnow()
                return True
            await self.enqueue_cleanup(exc)
            await self._record_failure(operation, exc)
            return False
        return True

    async def _acknowledge_join_error(
        self, operation: AccessOperation, bot: Bot
    ) -> bool | TelegramAPIError:
        from app.subscriptions.join_requests import JoinRequestService

        await self.session.scalar(
            select(User.id).where(User.telegram_id == operation.telegram_id).with_for_update()
        )
        await self.session.refresh(operation)
        if operation.completed_at is not None:
            return True
        decision = await JoinRequestService(self.session).decide(
            telegram_id=operation.telegram_id,
            chat_id=operation.chat_id,
            invite_value=operation.invite_link,
        )
        if not decision.allowed:
            operation.completed_at = utcnow()
            operation.last_error = f"join_{decision.reason}"
            return True
        try:
            member = await bot.get_chat_member(operation.chat_id, operation.telegram_id)
        except TelegramAPIError as exc:
            return exc
        present = member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        } or (member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False))
        operation.completed_at = utcnow()
        operation.last_error = None if present else "join_request_missing"
        return True

    async def _other_entitlement(
        self, operation: AccessOperation, subscription: Subscription
    ) -> bool:
        subscriptions = await self.session.scalars(
            select(Subscription)
            .options(selectinload(Subscription.channel), selectinload(Subscription.invite_link))
            .where(
                Subscription.user_id == subscription.user_id,
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.expires_at > utcnow(),
            )
        )
        return any(
            s.channel.telegram_chat_id == operation.chat_id
            or (s.invite_link and s.invite_link.materials_channel_id == operation.chat_id)
            for s in subscriptions
        )

    async def process_pending(
        self, bot: Bot | None, limit: int = 50, *, time_budget_seconds: float = 30
    ) -> int:
        if bot is None:
            return 0
        completed = 0
        deadline = asyncio.get_running_loop().time() + time_budget_seconds
        for _ in range(limit):
            budget = deadline - asyncio.get_running_loop().time()
            if budget <= 0:
                break
            operation = await self.session.scalar(
                select(AccessOperation)
                .where(
                    AccessOperation.completed_at.is_(None),
                    AccessOperation.next_attempt_at <= utcnow(),
                )
                .order_by(
                    case(
                        (AccessOperation.kind == "remove_member", 0),
                        (AccessOperation.kind == "revoke_link", 1),
                        (AccessOperation.kind == "approve_join", 2),
                        (AccessOperation.kind == "provision", 3),
                        (AccessOperation.kind == "expiry_notice", 4),
                        else_=5,
                    ),
                    AccessOperation.next_attempt_at,
                    AccessOperation.id,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if operation is None:
                break
            operation.next_attempt_at = utcnow() + timedelta(minutes=5)
            await self.session.commit()
            try:
                async with asyncio.timeout(min(120, budget)):
                    completed += int(await self.execute(operation, bot))
            except TimeoutError:
                await self.session.refresh(operation)
                operation.attempts += 1
                operation.last_error = "TimeoutError"
                operation.next_attempt_at = utcnow() + timedelta(seconds=60)
            await self.session.commit()
        return completed
