from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.db.enums import PaymentStatus


@dataclass(frozen=True)
class InvoiceRequest:
    payment_id: int
    user_id: int
    plan_id: int
    amount: Decimal
    currency: str
    description: str
    idempotency_key: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class InvoiceResult:
    provider_payment_id: str
    invoice_url: str | None
    raw_payload: dict[str, Any]


@dataclass(frozen=True)
class PaymentStatusResult:
    provider_payment_id: str
    status: PaymentStatus
    amount: Decimal
    currency: str
    raw_payload: dict[str, Any]


class PaymentProvider(ABC):
    @abstractmethod
    async def create_invoice(self, request: InvoiceRequest) -> InvoiceResult:
        raise NotImplementedError

    @abstractmethod
    async def handle_webhook(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        raw_body: bytes,
    ) -> PaymentStatusResult:
        raise NotImplementedError

    @abstractmethod
    async def check_payment_status(self, provider_payment_id: str) -> PaymentStatusResult:
        raise NotImplementedError

    async def refund(self, provider_payment_id: str, amount: Decimal | None = None) -> bool:
        return False

    @abstractmethod
    def get_provider_name(self) -> str:
        raise NotImplementedError
