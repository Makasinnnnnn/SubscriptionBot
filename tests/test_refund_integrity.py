from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_payment_integrity import db as payment_db
from test_payment_integrity import payment, seed

from app.db.enums import PaymentStatus
from app.services.refund_service import RefundService

db = payment_db


def result(payment_id, *, refund_id="refund-1", value="10", currency="USD", status="succeeded"):
    return {
        "id": refund_id,
        "payment_id": payment_id,
        "status": status,
        "amount": {"value": value, "currency": currency},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["amount", "currency", "payment", "missing_id"])
async def test_creation_response_cannot_confirm_another_or_partial_refund(
    db, monkeypatch, mismatch
):
    user, _channel, plan = await seed(db)
    p = await payment(db, user, plan, "charge-1", provider="yookassa")
    response = result(p.provider_payment_id)
    if mismatch == "amount":
        response["amount"]["value"] = "1"
    if mismatch == "currency":
        response["amount"]["currency"] = "EUR"
    if mismatch == "payment":
        response["payment_id"] = "other-charge"
    if mismatch == "missing_id":
        response.pop("id")
    provider = SimpleNamespace(create_refund=AsyncMock(return_value=response))
    monkeypatch.setattr("app.services.refund_service.get_payment_provider", lambda _: provider)
    with pytest.raises(ValueError):
        await RefundService(db).request(p)
    assert p.status == PaymentStatus.PAID


@pytest.mark.asyncio
async def test_reconciliation_response_matches_stored_refund_id(db, monkeypatch):
    user, _channel, plan = await seed(db)
    p = await payment(db, user, plan, "charge-1", provider="yookassa")
    provider = SimpleNamespace(
        create_refund=AsyncMock(return_value=result(p.provider_payment_id, status="pending")),
        check_refund=AsyncMock(
            return_value=result(p.provider_payment_id, refund_id="other-refund")
        ),
    )
    monkeypatch.setattr("app.services.refund_service.get_payment_provider", lambda _: provider)
    assert await RefundService(db).request(p) is False
    with pytest.raises(ValueError):
        await RefundService(db).request(p)
    assert p.status == PaymentStatus.PAID


from app.payments.providers import CryptoBotProvider, ProviderAPIError


@pytest.mark.asyncio
async def test_recovery_rejects_duplicate_payload_across_invoice_pages(monkeypatch):
    provider = CryptoBotProvider("synthetic", "https://never-called.invalid")
    page = [{"invoice_id": 1, "payload": "42", "bot_invoice_url": "https://example.test/1"}]
    page += [{"invoice_id": i, "payload": "other"} for i in range(2, 1001)]
    next_page = [{"invoice_id": 1001, "payload": "42", "bot_invoice_url": "https://example.test/2"}]
    request = AsyncMock(side_effect=[{"items": page}, {"items": next_page}])
    monkeypatch.setattr(provider, "_request", request)
    with pytest.raises(ProviderAPIError, match="Multiple"):
        await provider.reconcile_invoice(42)
