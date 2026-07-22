from datetime import UTC, datetime, timedelta
from decimal import Decimal
import pytest
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.db.models import Channel, Payment, Plan, User
from app.payments.base import InvoiceResult
from app.payments.registry import get_payment_provider
from app.services.legal_service import LegalAcceptanceRequiredError, LegalService
from app.services.payment_service import PaymentService


class FakeProvider:
    async def create_invoice(self, request):
        return InvoiceResult(f"fake-{request.payment_id}", "https://pay.example.test", {})


@pytest.mark.asyncio
async def test_payment_requires_current_legal_acceptance(session, monkeypatch) -> None:
    settings = Settings(_env_file=None, app_env="test", legal_version="v1")
    monkeypatch.setattr("app.services.legal_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.payment_service.get_payment_provider", lambda _: FakeProvider())

    user = User(telegram_id=123)
    channel = Channel(telegram_chat_id=-1001, title="Channel")
    session.add_all([user, channel])
    await session.flush()
    plan = Plan(
        channel_id=channel.id,
        title="Month",
        duration_days=30,
        price=Decimal("100"),
        currency="RUB",
    )
    session.add(plan)
    await session.flush()

    with pytest.raises(LegalAcceptanceRequiredError):
        await PaymentService(session).create_payment(user, plan, "fake")

    acceptance = await LegalService(session, settings).accept(user)
    await session.commit()
    payment = await PaymentService(session).create_payment(user, plan, "fake")
    duplicate = await PaymentService(session).create_payment(user, plan, "fake")

    assert acceptance.legal_version == "v1"
    assert payment.id == duplicate.id
    assert len(list(await session.scalars(Payment.__table__.select()))) == 1


@pytest.mark.asyncio
async def test_new_legal_version_requires_acceptance_again(session) -> None:
    user = User(telegram_id=456)
    session.add(user)
    await session.flush()
    v1 = Settings(_env_file=None, app_env="test", legal_version="v1")
    v2 = Settings(_env_file=None, app_env="test", legal_version="v2")

    first = await LegalService(session, v1).accept(user)
    same = await LegalService(session, v1).accept(user)

    assert first.id == same.id
    assert await LegalService(session, v1).is_accepted(user.id)
    assert not await LegalService(session, v2).is_accepted(user.id)


def test_stars_use_provider_specific_price() -> None:
    plan = Plan(price=Decimal("4990"), currency="RUB", stars_price=500)

    assert PaymentService._provider_price(plan, "cryptobot") == (Decimal("4990"), "RUB")
    assert PaymentService._provider_price(plan, "telegram-stars") == (Decimal("500"), "XTR")


def test_production_configuration_rejects_unsafe_defaults() -> None:
    with pytest.raises(ValueError, match="Invalid production configuration"):
        Settings(_env_file=None, app_env="production")


def test_disabled_providers_do_not_require_credentials() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        enable_cryptobot=False,
        enable_yookassa=False,
    )
    assert not settings.enable_cryptobot


def test_disabled_provider_cannot_be_selected(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        enable_dev_payments=False,
        enable_telegram_stars=False,
        enable_cryptobot=False,
        enable_yookassa=False,
    )
    monkeypatch.setattr("app.payments.registry.get_settings", lambda: settings)

    with pytest.raises(ValueError, match="disabled or unknown"):
        get_payment_provider("cryptobot")


def test_valid_production_configuration_is_accepted() -> None:
    settings = Settings(
        _env_file=None,
        app_env="production",
        app_url="https://propgate.ru",
        allowed_hosts=["propgate.ru"],
        bot_token="configured-bot-token",
        admin_ids=[1],
        materials_channel_id=-1001,
        enable_dev_payments=False,
        database_url="postgresql+asyncpg://app:strong-db-password@postgres:5432/app",
        webhook_secret="w" * 40,
        admin_password="a-strong-admin-password",
        support_ids=[2],
    )

    assert settings.app_env == "production"


@pytest.mark.asyncio
async def test_stale_or_repriced_pending_invoice_is_replaced(session, monkeypatch) -> None:
    settings = Settings(_env_file=None, app_env="test", legal_version="v1")
    monkeypatch.setattr("app.services.legal_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.payment_service.get_payment_provider", lambda _: FakeProvider())
    user = User(telegram_id=777)
    channel = Channel(telegram_chat_id=-2001, title="Channel")
    session.add_all([user, channel])
    await session.flush()
    plan = Plan(
        channel_id=channel.id,
        title="Month",
        duration_days=30,
        price=Decimal("100"),
        currency="RUB",
    )
    session.add(plan)
    await session.flush()
    await LegalService(session, settings).accept(user)
    old = Payment(
        user_id=user.id,
        plan_id=plan.id,
        provider="fake",
        provider_payment_id="old-invoice",
        amount=Decimal("100"),
        currency="RUB",
        idempotency_key="old-idempotency",
        created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2),
    )
    session.add(old)
    await session.commit()

    plan.price = Decimal("200")
    await session.commit()
    replacement = await PaymentService(session).create_payment(user, plan, "fake")

    assert replacement.id != old.id
    assert replacement.amount == Decimal("200")
    assert old.status.value == "expired"


@pytest.mark.asyncio
async def test_external_payment_id_is_unique_per_provider(session) -> None:
    user = User(telegram_id=888)
    channel = Channel(telegram_chat_id=-3001, title="Channel")
    session.add_all([user, channel])
    await session.flush()
    plan = Plan(
        channel_id=channel.id,
        title="Month",
        duration_days=30,
        price=Decimal("100"),
        currency="RUB",
    )
    session.add(plan)
    await session.flush()
    common = {
        "user_id": user.id,
        "plan_id": plan.id,
        "provider_payment_id": "same-external-id",
        "amount": Decimal("100"),
        "currency": "RUB",
    }
    session.add_all(
        [
            Payment(provider="cryptobot", idempotency_key="one", **common),
            Payment(provider="yookassa", idempotency_key="two", **common),
        ]
    )
    await session.commit()
    session.add(Payment(provider="cryptobot", idempotency_key="three", **common))

    with pytest.raises(IntegrityError):
        await session.flush()
