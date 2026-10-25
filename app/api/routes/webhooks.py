import ipaddress
import json
import logging
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.rate_limit import SlidingWindowLimiter
from app.db.session import get_session
from app.payments.providers import ProviderAPIError, WebhookVerificationError
from app.services.webhook_service import WebhookService

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)
webhook_limiter = SlidingWindowLimiter()


async def _process(
    provider: str,
    request: Request,
    session: AsyncSession,
    secret: str | None,
) -> dict[str, str]:
    settings = get_settings()
    if not settings.webhook_secret:
        raise HTTPException(status_code=503, detail="Webhook secret is not configured")
    if secret is None or not secrets.compare_digest(
        secret.encode(), settings.webhook_secret.encode()
    ):
        logger.warning("Webhook path secret rejected provider=%s", provider)
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    client_host = request.client.host if request.client else "unknown"
    if not await webhook_limiter.allow(
        f"{provider}:{client_host}",
        limit=300,
        window_seconds=60,
    ):
        raise HTTPException(
            status_code=429,
            detail="Webhook rate limit exceeded",
            headers={"Retry-After": "60"},
        )
    if provider == "yookassa" and settings.yookassa_allowed_ips:
        client_ip = _client_ip(request)
        if not client_ip or not _ip_allowed(client_ip, settings.yookassa_allowed_ips):
            logger.warning("YooKassa webhook IP rejected")
            raise HTTPException(status_code=403, detail="Webhook source IP is not allowed")

    content_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="Webhook Content-Type must be application/json")
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
        if declared_length < 0:
            raise HTTPException(status_code=400, detail="Invalid Content-Length")
        if declared_length > settings.webhook_max_body_bytes:
            raise HTTPException(status_code=413, detail="Webhook body is too large")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > settings.webhook_max_body_bytes:
            raise HTTPException(status_code=413, detail="Webhook body is too large")
        body.extend(chunk)
    raw_body = bytes(body)
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Webhook JSON must be an object")

    headers = {key.lower(): value for key, value in request.headers.items()}
    try:
        return await WebhookService(session).process(provider, payload, headers, raw_body)
    except WebhookVerificationError as exc:
        raise HTTPException(status_code=403, detail="Webhook verification failed") from exc
    except ProviderAPIError as exc:
        raise HTTPException(status_code=502, detail="Payment provider verification failed") from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Unknown payment provider") from exc


@router.post("/yookassa")
async def yookassa_webhook_without_secret() -> None:
    raise HTTPException(status_code=403, detail="Secret webhook path is required")


@router.post("/yookassa/{secret}")
async def yookassa_webhook(
    secret: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    return await _process("yookassa", request, session, secret)


@router.post("/cryptobot")
async def cryptobot_webhook_without_secret() -> None:
    raise HTTPException(status_code=403, detail="Secret webhook path is required")


@router.post("/cryptobot/{secret}")
async def cryptobot_webhook(
    secret: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    return await _process("cryptobot", request, session, secret)


def _client_ip(request: Request) -> str | None:
    direct_ip = request.client.host if request.client else None
    settings = get_settings()
    if direct_ip and _ip_allowed(direct_ip, settings.trusted_proxy_ips):
        forwarded = request.headers.get("cf-connecting-ip", "")
        if not forwarded:
            forwarded = request.headers.get("x-forwarded-for", "").split(",", maxsplit=1)[0]
        forwarded = forwarded.strip()
        return forwarded or direct_ip
    return direct_ip


def _ip_allowed(value: str, allowed: list[str]) -> bool:
    if not allowed:
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    for item in allowed:
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError:
            continue
        if address in network:
            return True
    return False
