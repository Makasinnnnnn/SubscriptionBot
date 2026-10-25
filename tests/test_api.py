from base64 import b64encode
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.main import _request_id, _safe_path, app
from app.db.session import get_session
from app.services.webhook_service import WebhookService


@pytest.mark.asyncio
async def test_health_and_database_health(session) -> None:
    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/health")).json() == {"status": "ok"}
            assert (await client.get("/health/db")).json() == {"status": "ok"}
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_webhook_secret_is_required_and_accepted(session, monkeypatch) -> None:
    async def override_session():
        yield session

    async def fake_process(self, provider_name, payload, headers, raw_body):
        return {"status": "ok"}

    settings = SimpleNamespace(
        webhook_secret="secret",
        webhook_max_body_bytes=1000,
        yookassa_allowed_ips=[],
        trusted_proxy_ips=[],
    )
    monkeypatch.setattr("app.api.routes.webhooks.get_settings", lambda: settings)
    monkeypatch.setattr(WebhookService, "process", fake_process)
    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            rejected = await client.post("/webhooks/cryptobot", json={"update_id": 1})
            accepted = await client.post(
                "/webhooks/cryptobot/secret",
                json={"update_id": 1},
            )
        assert rejected.status_code == 403
        assert accepted.status_code == 200
        assert accepted.json() == {"status": "ok"}
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_web_admin_requires_basic_auth(session, monkeypatch) -> None:
    async def override_session():
        yield session

    settings = SimpleNamespace(admin_username="admin", admin_password="strong-password")
    monkeypatch.setattr("app.admin.routes.get_settings", lambda: settings)
    app.dependency_overrides[get_session] = override_session
    authorization = b64encode(b"admin:strong-password").decode()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            rejected = await client.get("/admin/web")
            accepted = await client.get(
                "/admin/web",
                headers={"Authorization": f"Basic {authorization}"},
            )
        assert rejected.status_code == 401
        assert accepted.status_code == 200
        assert "PropGate Admin" in accepted.text
    finally:
        app.dependency_overrides.clear()


def test_webhook_secret_is_masked_in_access_logs() -> None:
    assert _safe_path("/webhooks/cryptobot/top-secret") == "/webhooks/cryptobot/***"
    assert _safe_path("/health") == "/health"


@pytest.mark.asyncio
async def test_webhook_rejects_wrong_content_type_and_oversized_body(
    session,
    monkeypatch,
) -> None:
    async def override_session():
        yield session

    settings = SimpleNamespace(
        webhook_secret="secret",
        webhook_max_body_bytes=4,
        yookassa_allowed_ips=[],
        trusted_proxy_ips=[],
    )
    monkeypatch.setattr("app.api.routes.webhooks.get_settings", lambda: settings)
    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            wrong_type = await client.post(
                "/webhooks/cryptobot/secret",
                content=b"{}",
                headers={"content-type": "text/plain"},
            )
            too_large = await client.post(
                "/webhooks/cryptobot/secret",
                content=b'{"a":1}',
                headers={"content-type": "application/json"},
            )
        assert wrong_type.status_code == 415
        assert too_large.status_code == 413
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_security_headers_and_request_id_sanitization() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health", headers={"x-request-id": "bad\nvalue"})

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-request-id"] != "bad\nvalue"
    assert _request_id("safe-request_1") == "safe-request_1"
