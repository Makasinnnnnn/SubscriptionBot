from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_payment_integrity import db as payment_db
from test_payment_integrity import payment, seed

from app.db.enums import PaymentStatus
from app.db.models import AccessOperation
from app.payments.base import InvoiceResult, PaymentStatusResult
from app.services.payment_service import PaymentService

db = payment_db


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["status", "recover"])
async def test_paid_fact_always_commits_with_provision_task(db, monkeypatch, route):
    user, _channel, plan = await seed(db)
    p = await payment(
        db, user, plan, "outbox-charge", provider="cryptobot", status=PaymentStatus.PENDING
    )
    provider = SimpleNamespace(
        check_payment_status=AsyncMock(
            return_value=PaymentStatusResult(
                "outbox-charge", PaymentStatus.PAID, Decimal(10), "USD", {}
            )
        ),
        reconcile_invoice=AsyncMock(
            return_value=InvoiceResult("outbox-charge", "https://example.test", {})
        ),
    )
    monkeypatch.setattr("app.services.payment_service.get_payment_provider", lambda _: provider)
    if route == "recover":
        p.provider_payment_id = None
        p.metadata_json = {"invoice_state": "unknown"}
        await db.commit()
        await PaymentService(db).create_payment(user, plan, "cryptobot")
    else:
        await PaymentService(db).refresh_status(p)
    assert p.status == PaymentStatus.PAID
    task = await db.scalar(select(AccessOperation).where(AccessOperation.payment_id == p.id))
    assert task is not None and task.kind == "provision"
