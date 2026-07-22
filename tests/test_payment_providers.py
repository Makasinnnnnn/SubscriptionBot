import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.db.enums import PaymentStatus
from app.payments.base import InvoiceRequest
from app.payments.providers import (
    CryptoBotProvider,
    DevPaymentProvider,
    TelegramStarsProvider,
    WebhookVerificationError,
    _map_cryptobot_status,
    _map_yookassa_status,
)


@pytest.mark.asyncio
async def test_telegram_stars_provider_creates_invoice() -> None:
    provider = TelegramStarsProvider(provider_token="")
    result = await provider.create_invoice(
        InvoiceRequest(
            payment_id=1,
            user_id=1,
            plan_id=1,
            amount=Decimal("100"),
            currency="XTR",
            description="Test",
            idempotency_key="idem",
            metadata={},
        )
    )

    assert provider.get_provider_name() == "telegram-stars"
    assert result.provider_payment_id == "telegram-stars:idem"


@pytest.mark.asyncio
async def test_telegram_stars_rejects_external_webhook() -> None:
    provider = TelegramStarsProvider(provider_token="")
    with pytest.raises(WebhookVerificationError):
        await provider.handle_webhook(
            {"pre_checkout_query": {"id": "p1", "total_amount": 100}},
            {},
            b"{}",
        )


@pytest.mark.asyncio
async def test_cryptobot_webhook_verifies_signature() -> None:
    token = "123:test-token"
    request_date = datetime.now(UTC).isoformat()
    payload = {
        "update_id": 1,
        "update_type": "invoice_paid",
        "request_date": request_date,
        "payload": {
            "invoice_id": 42,
            "status": "paid",
            "amount": "10.00",
            "fiat": "RUB",
            "payload": "private-order-data",
            "bot_invoice_url": "https://sensitive.example/invoice",
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    secret = hashlib.sha256(token.encode("utf-8")).digest()
    signature = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()

    provider = CryptoBotProvider(token=token, api_url="https://pay.crypt.bot/api")
    result = await provider.handle_webhook(
        payload,
        {"crypto-pay-api-signature": signature},
        raw_body,
    )

    assert result.provider_payment_id == "42"
    assert result.status == PaymentStatus.PAID
    assert result.amount == Decimal("10.00")
    assert result.currency == "RUB"
    assert result.raw_payload["invoice"] == {
        "invoice_id": 42,
        "status": "paid",
        "amount": "10.00",
        "fiat": "RUB",
    }


@pytest.mark.asyncio
async def test_cryptobot_webhook_rejects_bad_signature() -> None:
    provider = CryptoBotProvider(token="123:test-token", api_url="https://pay.crypt.bot/api")

    with pytest.raises(WebhookVerificationError):
        await provider.handle_webhook(
            {"payload": {"invoice_id": 42, "status": "paid", "amount": "10.00", "fiat": "RUB"}},
            {"crypto-pay-api-signature": "bad"},
            b"{}",
        )


@pytest.mark.asyncio
async def test_cryptobot_webhook_rejects_stale_request() -> None:
    token = "123:test-token"
    request_date = (datetime.now(UTC) - timedelta(days=4)).isoformat()
    raw_body = (
        '{"update_type":"invoice_paid","request_date":"'
        + request_date
        + '","payload":{"invoice_id":42,"status":"paid","amount":"10.00","fiat":"RUB"}}'
    ).encode()
    secret = hashlib.sha256(token.encode()).digest()
    signature = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    provider = CryptoBotProvider(token=token, api_url="https://pay.crypt.bot/api")

    with pytest.raises(WebhookVerificationError):
        await provider.handle_webhook(
            {
                "update_type": "invoice_paid",
                "request_date": request_date,
                "payload": {
                    "invoice_id": 42,
                    "status": "paid",
                    "amount": "10.00",
                    "fiat": "RUB",
                },
            },
            {"crypto-pay-api-signature": signature},
            raw_body,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("update_type", "invoice_status"),
    [("invoice_created", "paid"), ("invoice_paid", "active")],
)
async def test_cryptobot_webhook_rejects_wrong_event_or_status(
    update_type: str,
    invoice_status: str,
) -> None:
    token = "123:test-token"
    payload = {
        "update_type": update_type,
        "request_date": datetime.now(UTC).isoformat(),
        "payload": {
            "invoice_id": 42,
            "status": invoice_status,
            "amount": "10.00",
            "fiat": "RUB",
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    secret = hashlib.sha256(token.encode()).digest()
    signature = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    provider = CryptoBotProvider(token=token, api_url="https://pay.crypt.bot/api")

    with pytest.raises(WebhookVerificationError):
        await provider.handle_webhook(
            payload,
            {"crypto-pay-api-signature": signature},
            raw_body,
        )


@pytest.mark.asyncio
async def test_cryptobot_webhook_requires_timestamp_and_valid_amount() -> None:
    token = "123:test-token"
    provider = CryptoBotProvider(token=token, api_url="https://pay.crypt.bot/api")
    payload = {
        "update_type": "invoice_paid",
        "payload": {"invoice_id": 42, "status": "paid", "amount": "invalid", "fiat": "RUB"},
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(
        hashlib.sha256(token.encode()).digest(), raw_body, hashlib.sha256
    ).hexdigest()
    with pytest.raises(WebhookVerificationError, match="request_date"):
        await provider.handle_webhook(
            payload,
            {"crypto-pay-api-signature": signature},
            raw_body,
        )

    payload["request_date"] = datetime.now(UTC).isoformat()
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(
        hashlib.sha256(token.encode()).digest(), raw_body, hashlib.sha256
    ).hexdigest()
    with pytest.raises(WebhookVerificationError, match="amount"):
        await provider.handle_webhook(
            payload,
            {"crypto-pay-api-signature": signature},
            raw_body,
        )


@pytest.mark.asyncio
async def test_dev_provider_creates_local_confirmation_payment() -> None:
    provider = DevPaymentProvider()
    result = await provider.create_invoice(
        InvoiceRequest(
            payment_id=7,
            user_id=1,
            plan_id=1,
            amount=Decimal("100"),
            currency="RUB",
            description="Test",
            idempotency_key="dev-idem",
            metadata={},
        )
    )
    assert result.provider_payment_id == "dev:7"
    assert result.invoice_url is None


def test_provider_status_mapping() -> None:
    assert _map_cryptobot_status("paid") == PaymentStatus.PAID
    assert _map_cryptobot_status("expired") == PaymentStatus.EXPIRED
    assert _map_yookassa_status("succeeded") == PaymentStatus.PAID
    assert _map_yookassa_status("canceled") == PaymentStatus.FAILED
