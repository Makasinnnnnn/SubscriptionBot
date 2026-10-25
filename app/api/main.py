import logging
import re
from contextlib import asynccontextmanager
from uuid import uuid4

from aiogram import Bot
from fastapi import FastAPI, Request
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.admin.routes import router as admin_router
from app.api.routes import health, webhooks
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.core.request_context import request_id_context
from app.services.admin_notification_service import notify_runtime

setup_logging()
settings = get_settings()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    bot = Bot(settings.bot_token) if settings.bot_token else None
    try:
        await notify_runtime(bot, "API", "запущен")
        yield
    finally:
        await notify_runtime(bot, "API", "остановлен")
        if bot is not None:
            await bot.session.close()


production = settings.app_env == "production"
app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
    debug=settings.debug,
    docs_url=None if production else "/docs",
    redoc_url=None if production else "/redoc",
    openapi_url=None if production else "/openapi.json",
)
if production:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
app.include_router(health.router)
app.include_router(webhooks.router)
app.include_router(admin_router)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    request_id = _request_id(request.headers.get("x-request-id"))
    token = request_id_context.set(request_id)
    try:
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["x-frame-options"] = "DENY"
        response.headers["referrer-policy"] = "no-referrer"
        response.headers["permissions-policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith(("/admin/", "/webhooks/")):
            response.headers["cache-control"] = "no-store"
        logger.info(
            "HTTP request method=%s path=%s status=%s",
            request.method,
            _safe_path(request.url.path),
            response.status_code,
        )
        return response
    except Exception:
        logger.exception(
            "HTTP request failed method=%s path=%s",
            request.method,
            _safe_path(request.url.path),
        )
        raise
    finally:
        request_id_context.reset(token)


def _safe_path(path: str) -> str:
    parts = path.strip("/").split("/")
    if len(parts) >= 3 and parts[0] == "webhooks":
        return f"/webhooks/{parts[1]}/***"
    return path


def _request_id(value: str | None) -> str:
    if value and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value):
        return value
    return str(uuid4())
