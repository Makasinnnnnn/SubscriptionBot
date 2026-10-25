from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from starlette.requests import Request

from app.admin.pricing import validate_plan_price
from app.bot.handlers import admin, admin_ui
from app.db.enums import PaymentStatus
from app.db.models import Admin, AdminChannelAccess, Channel, Payment, Plan, User
from app.payments.providers import CryptoBotProvider, WebhookVerificationError


async def scoped_fixture(session, monkeypatch):
    primary = Channel(telegram_chat_id=-100820001, title="Scoped channel")
    forbidden = Channel(telegram_chat_id=-100820002, title="Private channel")
    operator = Admin(telegram_id=820001, role="admin", all_channels=False)
    other_operator = Admin(telegram_id=820002, role="admin", all_channels=False)
    user = User(telegram_id=820003)
    session.add_all([primary, forbidden, operator, other_operator, user])
    await session.flush()
    session.add_all(
        [
            AdminChannelAccess(admin_id=operator.id, channel_id=primary.id),
            AdminChannelAccess(admin_id=other_operator.id, channel_id=forbidden.id),
        ]
    )
    plan = Plan(
        channel_id=primary.id,
        title="Test",
        duration_days=30,
        price=Decimal(20),
        currency="RUB",
        stars_price=20,
    )
    session.add(plan)
    await session.flush()
    payment = Payment(
        user_id=user.id,
        plan_id=plan.id,
        provider="telegram-stars",
        amount=Decimal(20),
        currency="XTR",
        status=PaymentStatus.PAID,
        purchased_channel_id=primary.id,
        purchased_duration_days=30,
        idempotency_key="scoped-fixture",
    )
    session.add(payment)
    await session.commit()

    @asynccontextmanager
    async def factory():
        yield session

    monkeypatch.setattr(admin_ui, "async_session_factory", factory)
    monkeypatch.setattr(admin, "async_session_factory", factory)
    return primary, forbidden, plan, payment


@pytest.mark.asyncio
async def test_scoped_stars_dashboard_does_not_fetch_global_transactions(session, monkeypatch):
    await scoped_fixture(session, monkeypatch)
    getter = AsyncMock(
        return_value=(
            SimpleNamespace(amount=9999, nanostar_amount=None),
            SimpleNamespace(
                transactions=[
                    SimpleNamespace(date=datetime.now(UTC), amount=9999, id="private-charge")
                ]
            ),
        )
    )
    monkeypatch.setattr(admin_ui, "_telegram_stars_data", getter)
    editor = AsyncMock()
    monkeypatch.setattr(admin_ui, "_edit", editor)
    callback = SimpleNamespace(from_user=SimpleNamespace(id=820001), answer=AsyncMock())
    await admin_ui.stars_dashboard(callback)
    getter.assert_not_awaited()
    assert "private-charge" not in editor.await_args.args[1]
    assert "20 XTR" in editor.await_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(("actor", "expected_edit"), [(820001, True), (820002, False)])
async def test_stars_refund_selection_uses_purchased_channel(
    session, monkeypatch, actor, expected_edit
):
    _, forbidden, plan, payment = await scoped_fixture(session, monkeypatch)
    payment_id = payment.id
    plan.channel_id = forbidden.id
    await session.commit()
    session.expunge_all()
    editor = AsyncMock()
    monkeypatch.setattr(admin_ui, "_edit", editor)
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=actor), answer=AsyncMock(), data=f"adm:starrefund:{payment_id}"
    )
    await admin_ui.stars_refund_request(callback)
    assert bool(editor.await_count) == expected_edit


@pytest.mark.parametrize("value", ["1.234567", "0.000001"])
def test_admin_price_parser_preserves_usdt_precision(value):
    assert admin_ui._price(value) == Decimal(value)


@pytest.mark.parametrize(
    ("value", "currency"), [("1.234", "RUB"), ("1.1234567", "USDT"), ("1.25", "XTR")]
)
def test_plan_price_precision_error_is_localized(value, currency):
    with pytest.raises(ValueError) as error:
        validate_plan_price(Decimal(value), currency)
    assert "цен" in str(error.value) or "знаков" in str(error.value)


@pytest.mark.asyncio
async def test_new_xtr_plan_has_payable_stars_price(session, monkeypatch):
    primary, _, _, _ = await scoped_fixture(session, monkeypatch)
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=820001),
        answer=AsyncMock(),
        text=f"/admin_add_plan {primary.id} | Stars | 30 | 25 | XTR",
    )
    await admin.admin_add_plan(message)
    plan = await session.scalar(select(Plan).where(Plan.title == "Stars"))
    assert plan is not None and plan.stars_price == 25


@pytest.mark.asyncio
async def test_xtr_plan_wizard_initializes_stars_price(session, monkeypatch):
    primary, _, _, _ = await scoped_fixture(session, monkeypatch)
    state = SimpleNamespace(
        get_data=AsyncMock(
            return_value={
                "channel_id": primary.id,
                "title": "Wizard Stars",
                "duration_days": 30,
                "price": "26",
            }
        ),
        clear=AsyncMock(),
    )
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=820001), data="adm:currency:XTR", answer=AsyncMock()
    )
    monkeypatch.setattr(admin_ui, "_show_plan", AsyncMock())
    await admin_ui.add_plan_currency(callback, state)
    plan = await session.scalar(select(Plan).where(Plan.title == "Wizard Stars"))
    assert plan is not None and plan.stars_price == 26


@pytest.mark.asyncio
@pytest.mark.parametrize(("price", "currency"), [("1", "BANANA"), ("1.25", "XTR")])
async def test_plan_command_rejects_unpayable_currency_or_fractional_stars(
    session, monkeypatch, price, currency
):
    primary, _, _, _ = await scoped_fixture(session, monkeypatch)
    before = await session.scalar(select(func.count(Plan.id)))
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=820001),
        answer=AsyncMock(),
        text=f"/admin_add_plan {primary.id} | New | 30 | {price} | {currency}",
    )
    await admin.admin_add_plan(message)
    assert await session.scalar(select(func.count(Plan.id))) == before


@pytest.mark.asyncio
async def test_unicode_wrong_webhook_path_secret_is_rejected_without_server_error(monkeypatch):
    from app.api.routes import webhooks

    monkeypatch.setattr(webhooks, "get_settings", lambda: SimpleNamespace(webhook_secret="audit"))
    request = Request({"type": "http", "headers": [], "client": ("192.0.2.1", 1)})
    with pytest.raises(HTTPException) as error:
        await webhooks._process("cryptobot", request, None, "🔑")
    assert error.value.status_code == 403


def test_unicode_crypto_signature_is_rejected_as_verification_error():
    provider = CryptoBotProvider(token="audit-only", api_url="https://audit.invalid")
    with pytest.raises(WebhookVerificationError):
        provider._verify_signature({"crypto-pay-api-signature": "🔑"}, b"{}")


def test_settings_validation_error_does_not_echo_credentials():
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, app_env="invalid", admin_password="pa55word")
    assert "pa55word" not in str(error.value)


def test_settings_repr_does_not_echo_database_credentials():
    from app.core.config import Settings

    settings = Settings(
        _env_file=None, database_url="postgresql+asyncpg://audit:audit-db-sensitive@db/test"
    )
    assert "audit-db-sensitive" not in repr(settings)


@pytest.mark.asyncio
async def test_promo_wizard_handles_code_claimed_before_final_step(session, monkeypatch):
    from datetime import timedelta

    from app.bot.handlers import admin_settings
    from app.db.models import PromoCode

    _, _, plan, _ = await scoped_fixture(session, monkeypatch)
    plan_id = plan.id
    now = datetime.now(UTC).replace(tzinfo=None)
    session.add(
        PromoCode(
            code="RACE",
            plan_id=plan_id,
            valid_from=now,
            valid_until=now + timedelta(days=1),
            discount_percent=20,
            max_redemptions=5,
            per_user_limit=1,
        )
    )
    await session.commit()
    session.expunge_all()

    @asynccontextmanager
    async def factory():
        yield session

    monkeypatch.setattr(admin_settings, "async_session_factory", factory)
    state = SimpleNamespace(
        get_data=AsyncMock(
            return_value={
                "plan_id": plan_id,
                "code": "RACE",
                "discount_percent": 20,
                "valid_from": now.isoformat(),
                "valid_until": (now + timedelta(days=1)).isoformat(),
            }
        ),
        set_state=AsyncMock(),
        clear=AsyncMock(),
    )
    message = SimpleNamespace(from_user=SimpleNamespace(id=820001), text="5", answer=AsyncMock())
    await admin_settings.promo_create_limit(message, state)
    state.set_state.assert_awaited_once_with(admin_settings.PromoAdminForm.code)
    assert "уже существует" in message.answer.await_args.args[0]
    assert await session.scalar(select(func.count(PromoCode.id))) == 1


@pytest.mark.asyncio
async def test_manual_access_error_does_not_echo_internal_exception(session, monkeypatch):
    primary, _, _, _ = await scoped_fixture(session, monkeypatch)
    monkeypatch.setattr(
        admin_ui.SubscriptionService,
        "grant_manual",
        AsyncMock(side_effect=ValueError("internal-private-detail")),
    )
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"channel_id": primary.id, "telegram_id": 820003}),
        clear=AsyncMock(),
    )
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=820001), text="30", answer=AsyncMock(), bot=SimpleNamespace()
    )
    await admin_ui.grant_days_received(message, state)
    reply = message.answer.await_args.args[0]
    assert "internal-private-detail" not in reply
    assert "Проверьте" in reply
