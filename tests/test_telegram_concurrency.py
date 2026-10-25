import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import test_payment_concurrency as concurrency_helpers
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from sqlalchemy import select
from test_subscription_service import FakeBot
from test_telegram_workflow_verification import seed_access

from app.bot.handlers import subscriptions as handlers
from app.db.enums import SubscriptionStatus
from app.db.models import AccessOperation, Subscription, User
from app.services.access_operation_service import AccessOperationService
from app.subscriptions.service import SubscriptionService

postgres_factory = concurrency_helpers.postgres_factory


async def queued_removal(factory):
    async with factory() as session:
        sub = await seed_access(session)
        sub.status = SubscriptionStatus.CANCELLED
        operation = await AccessOperationService(session).enqueue(
            "verify:remove",
            "remove_member",
            subscription_id=sub.id,
            telegram_id=500,
            chat_id=-1001,
            expires_at_snapshot=sub.expires_at,
        )
        await session.commit()
        return sub.id, operation.id


@pytest.mark.asyncio
async def test_postgres_second_worker_cannot_duplicate_claim(postgres_factory):
    _, operation_id = await queued_removal(postgres_factory)
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedBot(FakeBot):
        async def ban_chat_member(self, chat_id, user_id):
            await super().ban_chat_member(chat_id, user_id)
            entered.set()
            await release.wait()

    bot = PausedBot()

    async def work():
        async with postgres_factory() as session:
            return await AccessOperationService(session).process_pending(bot, limit=1)

    task = asyncio.create_task(work())
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        assert await asyncio.wait_for(work(), timeout=5) == 0
    finally:
        release.set()
    assert await asyncio.wait_for(task, timeout=10) == 1
    assert bot.banned == [(-1001, 500)]
    async with postgres_factory() as session:
        assert (await session.get(AccessOperation, operation_id)).completed_at is not None


@pytest.mark.asyncio
async def test_postgres_expired_claim_rechecks_completion_under_user_lock(postgres_factory):
    _, operation_id = await queued_removal(postgres_factory)
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedBot(FakeBot):
        async def ban_chat_member(self, chat_id, user_id):
            await super().ban_chat_member(chat_id, user_id)
            entered.set()
            await release.wait()

    bot = PausedBot()

    async def work():
        async with postgres_factory() as session:
            return await AccessOperationService(session).process_pending(bot, limit=1)

    first = asyncio.create_task(work())
    second = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        async with postgres_factory() as session:
            operation = await session.get(AccessOperation, operation_id)
            operation.next_attempt_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(
                seconds=1
            )
            await session.commit()
        second = asyncio.create_task(work())
        await asyncio.sleep(0.05)
        assert not second.done()
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=10)
    assert bot.banned == [(-1001, 500)]


@pytest.mark.asyncio
async def test_postgres_renewal_wins_pending_removal(postgres_factory):
    sub_id, _ = await queued_removal(postgres_factory)
    bot = FakeBot()
    async with postgres_factory() as renewal:
        await renewal.scalar(select(User.id).where(User.telegram_id == 500).with_for_update())
        sub = await renewal.get(Subscription, sub_id)
        sub.status = SubscriptionStatus.ACTIVE
        sub.expires_at += timedelta(days=3)
        await renewal.flush()

        async def work():
            async with postgres_factory() as session:
                return await AccessOperationService(session).process_pending(bot, limit=1)

        task = asyncio.create_task(work())
        await asyncio.sleep(0.05)
        assert not task.done()
        await renewal.commit()
    await asyncio.wait_for(task, timeout=10)
    assert bot.banned == []


@pytest.mark.asyncio
async def test_postgres_expiry_skips_locked_renewal_and_retains_new_period(postgres_factory):
    async with postgres_factory() as session:
        sub = await seed_access(session, minutes=-1)
        sub_id = sub.id
    async with postgres_factory() as renewal:
        await SubscriptionService(renewal).extend_manual(sub_id, 3, FakeBot())
        async with postgres_factory() as expiry:
            assert (
                await asyncio.wait_for(
                    SubscriptionService(expiry).expire_due_subscriptions(
                        FakeBot(), defer_telegram=True
                    ),
                    timeout=5,
                )
                == 0
            )
        await renewal.commit()
    async with postgres_factory() as session:
        sub = await session.get(Subscription, sub_id)
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.expires_at > datetime.now(UTC).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_postgres_join_approval_and_revoke_are_serialized(postgres_factory, monkeypatch):
    async with postgres_factory() as session:
        sub = await seed_access(session)
        sub_id = sub.id
    entered, release = asyncio.Event(), asyncio.Event()

    class MemberBot(FakeBot):
        def __init__(self):
            super().__init__()
            self.member = False

        async def approve_chat_join_request(self, **kwargs):
            entered.set()
            await release.wait()
            self.member = True

        async def ban_chat_member(self, chat_id, user_id):
            self.member = False
            await super().ban_chat_member(chat_id, user_id)

    bot = MemberBot()
    monkeypatch.setattr(handlers, "async_session_factory", postgres_factory)
    request = SimpleNamespace(
        invite_link=SimpleNamespace(invite_link="https://t.me/+verification"),
        from_user=SimpleNamespace(id=500),
        chat=SimpleNamespace(id=-1001),
        bot=bot,
        date=datetime.now(UTC),
    )
    approving = asyncio.create_task(handlers.handle_paid_join_request(request))
    revoking = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)

        async def revoke():
            async with postgres_factory() as session:
                await SubscriptionService(session).revoke(sub_id, bot)
                await session.commit()

        revoking = asyncio.create_task(revoke())
        await asyncio.sleep(0.05)
        assert not revoking.done()
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(approving, revoking), timeout=10)
    assert not bot.member
    async with postgres_factory() as session:
        assert (await session.get(Subscription, sub_id)).status == SubscriptionStatus.CANCELLED


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_before_retry", [False, True])
async def test_postgres_failed_join_persists_and_revalidates_after_restart(
    postgres_factory, monkeypatch, revoke_before_retry
):
    async with postgres_factory() as session:
        sub = await seed_access(session)
        sub_id = sub.id

    class JoinBot(FakeBot):
        def __init__(self, limited):
            super().__init__()
            self.limited = limited
            self.approved = 0
            self.declined = 0

        async def approve_chat_join_request(self, **kwargs):
            if self.limited:
                raise TelegramRetryAfter(method=None, message="test join limit", retry_after=120)
            self.approved += 1

        async def decline_chat_join_request(self, **kwargs):
            self.declined += 1

    monkeypatch.setattr(handlers, "async_session_factory", postgres_factory)
    request = SimpleNamespace(
        invite_link=SimpleNamespace(invite_link="https://t.me/+verification"),
        from_user=SimpleNamespace(id=500),
        chat=SimpleNamespace(id=-1001),
        bot=JoinBot(True),
        date=datetime.now(UTC),
    )
    await handlers.handle_paid_join_request(request)
    async with postgres_factory() as session:
        operation = await session.scalar(
            select(AccessOperation).where(AccessOperation.kind == "approve_join")
        )
        assert operation.completed_at is None
        assert operation.last_error == "TelegramRetryAfter"
        assert operation.next_attempt_at > datetime.now(UTC).replace(tzinfo=None) + timedelta(
            seconds=100
        )
        if revoke_before_retry:
            await SubscriptionService(session).revoke(sub_id, FakeBot())
        operation.next_attempt_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        await session.commit()
    healthy = JoinBot(False)
    async with postgres_factory() as session:
        assert await AccessOperationService(session).process_pending(healthy) == 1
    assert healthy.approved == int(not revoke_before_retry)
    assert healthy.declined == int(revoke_before_retry)


@pytest.mark.asyncio
@pytest.mark.parametrize("present", [True, False])
async def test_postgres_join_response_loss_is_verified_not_assumed(postgres_factory, present):
    async with postgres_factory() as session:
        sub = await seed_access(session)
        operation = await AccessOperationService(session).enqueue_join(
            500, -1001, "https://t.me/+verification", datetime.now(UTC), sub.id
        )
        await session.commit()

        class ResponseLostBot:
            async def approve_chat_join_request(self, **kwargs):
                raise TelegramBadRequest(
                    method=None,
                    message="USER_ALREADY_PARTICIPANT" if present else "HIDE_REQUESTER_MISSING",
                )

            async def get_chat_member(self, chat_id, user_id):
                assert (chat_id, user_id) == (-1001, 500)
                return SimpleNamespace(
                    status=ChatMemberStatus.MEMBER if present else ChatMemberStatus.LEFT
                )

        assert await AccessOperationService(session).execute(operation, ResponseLostBot())
        assert operation.completed_at is not None
        assert operation.last_error == (None if present else "join_request_missing")
