import asyncio
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.db.enums import PaymentStatus
from app.payments.base import InvoiceRequest, InvoiceResult, PaymentProvider, PaymentStatusResult


class ProviderNotConfiguredError(RuntimeError):
    pass


class ProviderAPIError(RuntimeError):
    pass


class WebhookVerificationError(RuntimeError):
    pass


class DevPaymentProvider(PaymentProvider):
    async def create_invoice(self, request: InvoiceRequest) -> InvoiceResult:
        return InvoiceResult(
            provider_payment_id=f"dev:{request.payment_id}",
            invoice_url=None,
            raw_payload={"status": "created", "provider": self.get_provider_name()},
        )

    async def handle_webhook(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        raw_body: bytes,
    ) -> PaymentStatusResult:
        return PaymentStatusResult(
            provider_payment_id=str(payload["provider_payment_id"]),
            status=PaymentStatus.PAID,
            amount=Decimal(str(payload["amount"])),
            currency=str(payload["currency"]).upper(),
            raw_payload=payload,
        )

    async def check_payment_status(self, provider_payment_id: str) -> PaymentStatusResult:
        return PaymentStatusResult(provider_payment_id, PaymentStatus.PENDING, Decimal("0"), "RUB", {})

    def get_provider_name(self) -> str:
        return "dev"


class YooKassaProvider(PaymentProvider):
    def __init__(self, shop_id: str, secret_key: str, api_url: str, return_url: str) -> None:
        self.shop_id = shop_id
        self.secret_key = secret_key
        self.api_url = api_url.rstrip("/")
        self.return_url = return_url

    async def create_invoice(self, request: InvoiceRequest) -> InvoiceResult:
        self._ensure_configured()
        body = {
            "amount": {
                "value": _format_amount(request.amount),
                "currency": request.currency.upper(),
            },
            "capture": True,
            "confirmation": {
                "type": "redirect",
                "return_url": self.return_url,
            },
            "description": request.description[:128],
            "metadata": {
                **request.metadata,
                "payment_id": str(request.payment_id),
                "user_id": str(request.user_id),
                "plan_id": str(request.plan_id),
            },
        }
        response = await self._request(
            "POST",
            "/payments",
            json=body,
            headers={"Idempotence-Key": request.idempotency_key[:64]},
        )
        confirmation = response.get("confirmation") or {}
        invoice_url = confirmation.get("confirmation_url")
        if not invoice_url:
            raise ProviderAPIError("YooKassa response does not contain confirmation_url")

        return InvoiceResult(
            provider_payment_id=str(response["id"]),
            invoice_url=str(invoice_url),
            raw_payload=_sanitize_yookassa_payment(response),
        )

    async def handle_webhook(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        raw_body: bytes,
    ) -> PaymentStatusResult:
        self._ensure_configured()
        obj = payload.get("object", payload)
        provider_payment_id = str(obj.get("id", ""))
        if not provider_payment_id:
            raise WebhookVerificationError("YooKassa webhook does not contain payment id")

        # YooKassa webhooks are verified by fetching the current object state with Basic Auth.
        return await self.check_payment_status(provider_payment_id)

    async def check_payment_status(self, provider_payment_id: str) -> PaymentStatusResult:
        self._ensure_configured()
        payment = await self._request("GET", f"/payments/{provider_payment_id}", retries=2)
        amount = payment.get("amount") or {}
        return PaymentStatusResult(
            provider_payment_id=str(payment["id"]),
            status=_map_yookassa_status(str(payment.get("status", ""))),
            amount=Decimal(str(amount.get("value", "0"))),
            currency=str(amount.get("currency", "RUB")).upper(),
            raw_payload=_sanitize_yookassa_payment(payment),
        )

    async def refund(self, provider_payment_id: str, amount: Decimal | None = None) -> bool:
        self._ensure_configured()
        payment_status = await self.check_payment_status(provider_payment_id)
        refund_amount = amount or payment_status.amount
        response = await self._request(
            "POST",
            "/refunds",
            json={
                "payment_id": provider_payment_id,
                "amount": {
                    "value": _format_amount(refund_amount),
                    "currency": payment_status.currency,
                },
            },
            headers={"Idempotence-Key": hashlib.sha256(provider_payment_id.encode()).hexdigest()[:64]},
        )
        return str(response.get("status")) == "succeeded"

    def get_provider_name(self) -> str:
        return "yookassa"

    def _ensure_configured(self) -> None:
        if not self.shop_id or not self.secret_key:
            raise ProviderNotConfiguredError("YooKassa credentials are not configured")

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        retries = int(kwargs.pop("retries", 0))
        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient(
                    base_url=self.api_url,
                    auth=(self.shop_id, self.secret_key),
                    timeout=20,
                ) as client:
                    response = await client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if attempt >= retries:
                    raise ProviderAPIError("YooKassa API transport error") from exc
                await asyncio.sleep(0.25 * (2**attempt))
                continue
            if response.status_code < 500 or attempt >= retries:
                break
            await asyncio.sleep(0.25 * (2**attempt))
        if response.status_code >= 400:
            raise ProviderAPIError(f"YooKassa API returned HTTP {response.status_code}")
        return response.json()


class CryptoBotProvider(PaymentProvider):
    def __init__(
        self,
        token: str,
        api_url: str,
        webhook_ttl_seconds: int = 259_200,
    ) -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.webhook_ttl_seconds = webhook_ttl_seconds

    async def create_invoice(self, request: InvoiceRequest) -> InvoiceResult:
        self._ensure_configured()
        currency = request.currency.upper()
        body: dict[str, Any] = {
            "amount": _format_amount(request.amount),
            "description": request.description[:1024],
            "payload": str(request.payment_id),
            "allow_comments": False,
            "allow_anonymous": False,
            "expires_in": 60 * 60,
        }
        if currency in {"USDT", "TON", "BTC", "ETH", "LTC", "BNB", "TRX", "USDC"}:
            body["currency_type"] = "crypto"
            body["asset"] = currency
        else:
            body["currency_type"] = "fiat"
            body["fiat"] = currency
            body["accepted_assets"] = "USDT,TON,BTC,ETH,LTC,BNB,TRX,USDC"

        invoice = await self._request("createInvoice", body)
        invoice_url = (
            invoice.get("bot_invoice_url")
            or invoice.get("web_app_invoice_url")
            or invoice.get("mini_app_invoice_url")
            or invoice.get("pay_url")
        )
        if not invoice_url:
            raise ProviderAPIError("CryptoBot response does not contain invoice URL")

        return InvoiceResult(
            provider_payment_id=str(invoice["invoice_id"]),
            invoice_url=str(invoice_url),
            raw_payload=_sanitize_cryptobot_invoice(invoice),
        )

    async def handle_webhook(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        raw_body: bytes,
    ) -> PaymentStatusResult:
        self._ensure_configured()
        self._verify_signature(headers, raw_body)
        self._verify_request_date(payload)
        if payload.get("update_type") != "invoice_paid":
            raise WebhookVerificationError("Unsupported CryptoBot webhook event")
        invoice = payload.get("payload")
        if not isinstance(invoice, dict):
            raise WebhookVerificationError("CryptoBot webhook payload must contain an invoice")
        provider_payment_id = str(invoice.get("invoice_id", ""))
        if not provider_payment_id:
            raise WebhookVerificationError("CryptoBot webhook does not contain invoice_id")
        if invoice.get("status") != "paid":
            raise WebhookVerificationError("CryptoBot invoice_paid webhook is not paid")
        try:
            amount = Decimal(str(invoice["amount"]))
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise WebhookVerificationError("CryptoBot webhook amount is invalid") from exc
        currency = str(invoice.get("fiat") or invoice.get("asset") or "").upper()
        if amount <= 0 or not currency:
            raise WebhookVerificationError("CryptoBot webhook amount or currency is invalid")
        return PaymentStatusResult(
            provider_payment_id=provider_payment_id,
            status=_map_cryptobot_status(str(invoice.get("status", ""))),
            amount=amount,
            currency=currency,
            raw_payload={
                "update_id": payload.get("update_id"),
                "update_type": payload.get("update_type"),
                "request_date": payload.get("request_date"),
                "invoice": _sanitize_cryptobot_invoice(invoice),
            },
        )

    async def check_payment_status(self, provider_payment_id: str) -> PaymentStatusResult:
        self._ensure_configured()
        response = await self._request(
            "getInvoices",
            {"invoice_ids": provider_payment_id, "count": 1},
            retries=2,
        )
        invoices = response.get("items") if isinstance(response, dict) else response
        if not invoices:
            raise ProviderAPIError(f"CryptoBot invoice not found: {provider_payment_id}")

        invoice = invoices[0]
        return PaymentStatusResult(
            provider_payment_id=str(invoice["invoice_id"]),
            status=_map_cryptobot_status(str(invoice.get("status", ""))),
            amount=Decimal(str(invoice.get("amount", "0"))),
            currency=str(invoice.get("fiat") or invoice.get("asset") or "USDT").upper(),
            raw_payload=_sanitize_cryptobot_invoice(invoice),
        )

    def get_provider_name(self) -> str:
        return "cryptobot"

    def _ensure_configured(self) -> None:
        if not self.token:
            raise ProviderNotConfiguredError("CryptoBot token is not configured")

    async def _request(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        retries: int = 0,
    ) -> Any:
        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient(
                    base_url=self.api_url,
                    headers={"Crypto-Pay-API-Token": self.token},
                    timeout=20,
                ) as client:
                    response = await client.post(f"/{method}", json=payload or {})
            except httpx.TransportError as exc:
                if attempt >= retries:
                    raise ProviderAPIError("CryptoBot API transport error") from exc
                await asyncio.sleep(0.25 * (2**attempt))
                continue
            if response.status_code < 500 or attempt >= retries:
                break
            await asyncio.sleep(0.25 * (2**attempt))
        if response.status_code >= 400:
            raise ProviderAPIError(f"CryptoBot API returned HTTP {response.status_code}")

        data = response.json()
        if not data.get("ok"):
            error = data.get("error") or {}
            error_name = error.get("name") if isinstance(error, dict) else "unknown"
            raise ProviderAPIError(f"CryptoBot API error: {error_name}")
        return data.get("result")

    def _verify_signature(self, headers: dict[str, str], raw_body: bytes) -> None:
        signature = headers.get("crypto-pay-api-signature")
        if not signature:
            raise WebhookVerificationError("Missing CryptoBot webhook signature")

        secret = hashlib.sha256(self.token.encode("utf-8")).digest()
        expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise WebhookVerificationError("Invalid CryptoBot webhook signature")

    def _verify_request_date(self, payload: dict[str, Any]) -> None:
        raw_date = payload.get("request_date")
        if not raw_date:
            raise WebhookVerificationError("Missing CryptoBot request_date")
        try:
            request_date = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
            if request_date.tzinfo is None:
                request_date = request_date.replace(tzinfo=UTC)
        except ValueError as exc:
            raise WebhookVerificationError("Invalid CryptoBot request_date") from exc
        now = datetime.now(UTC)
        if request_date > now + timedelta(minutes=5):
            raise WebhookVerificationError("CryptoBot request_date is in the future")
        if now - request_date > timedelta(seconds=self.webhook_ttl_seconds):
            raise WebhookVerificationError("CryptoBot webhook is outside replay window")


class TelegramStarsProvider(PaymentProvider):
    def __init__(self, provider_token: str) -> None:
        self.provider_token = provider_token

    async def create_invoice(self, request: InvoiceRequest) -> InvoiceResult:
        if request.currency.upper() != "XTR":
            raise ProviderAPIError("Telegram Stars plans must use XTR currency")
        return InvoiceResult(
            provider_payment_id=f"telegram-stars:{request.idempotency_key}",
            invoice_url=None,
            raw_payload={"status": "created", "provider": self.get_provider_name()},
        )

    async def handle_webhook(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        raw_body: bytes,
    ) -> PaymentStatusResult:
        raise WebhookVerificationError("Telegram Stars uses native Bot API payment updates")

    async def check_payment_status(self, provider_payment_id: str) -> PaymentStatusResult:
        return PaymentStatusResult(provider_payment_id, PaymentStatus.PENDING, Decimal("0"), "XTR", {})

    def get_provider_name(self) -> str:
        return "telegram-stars"


def _format_amount(amount: Decimal) -> str:
    return str(amount.quantize(Decimal("0.01")))


def _sanitize_cryptobot_invoice(invoice: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "invoice_id",
        "status",
        "amount",
        "fiat",
        "asset",
        "paid_amount",
        "paid_asset",
        "fee_asset",
        "fee_amount",
        "created_at",
        "paid_at",
        "expiration_date",
    )
    return {key: invoice[key] for key in allowed if key in invoice}


def _sanitize_yookassa_payment(payment: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "id",
        "status",
        "amount",
        "created_at",
        "paid",
        "captured_at",
        "expires_at",
        "refundable",
        "refunded_amount",
        "metadata",
    )
    return {key: payment[key] for key in allowed if key in payment}


def _map_yookassa_status(status: str) -> PaymentStatus:
    return {
        "succeeded": PaymentStatus.PAID,
        "canceled": PaymentStatus.FAILED,
        "pending": PaymentStatus.PENDING,
        "waiting_for_capture": PaymentStatus.PENDING,
    }.get(status, PaymentStatus.PENDING)


def _map_cryptobot_status(status: str) -> PaymentStatus:
    return {
        "active": PaymentStatus.PENDING,
        "paid": PaymentStatus.PAID,
        "expired": PaymentStatus.EXPIRED,
    }.get(status, PaymentStatus.PENDING)
