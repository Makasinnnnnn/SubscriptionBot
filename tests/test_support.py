from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.handlers.legal import _support_targets
from app.core.config import Settings


@pytest.mark.asyncio
async def test_support_ids_are_resolved_to_public_username_links(monkeypatch) -> None:
    settings = Settings(_env_file=None, app_env="test", support_ids=[101, 202])
    monkeypatch.setattr("app.bot.handlers.legal.get_settings", lambda: settings)
    bot = SimpleNamespace(
        get_chat=AsyncMock(
            side_effect=[
                SimpleNamespace(username="first_support"),
                SimpleNamespace(username="second_support"),
            ]
        )
    )

    contacts = await _support_targets(bot)

    assert contacts == [
        ("@first_support", "https://t.me/first_support"),
        ("@second_support", "https://t.me/second_support"),
    ]


@pytest.mark.asyncio
async def test_support_contact_is_fallback_when_id_has_no_username(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        support_ids=[101],
        support_contacts=["@fallback_support"],
    )
    monkeypatch.setattr("app.bot.handlers.legal.get_settings", lambda: settings)
    bot = SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(username=None)))

    contacts = await _support_targets(bot)

    assert contacts == [("@fallback_support", "https://t.me/fallback_support")]
