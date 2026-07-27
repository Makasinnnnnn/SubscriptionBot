from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.handlers.legal import (
    _support_back_keyboard,
    _support_menu_keyboard,
)
from app.core.config import Settings
from app.services.support_service import (
    SupportContact,
    SupportContactService,
    normalize_support_contact,
    support_contacts_from_settings,
)


def test_support_menu_and_child_screens_have_back_navigation() -> None:
    menu_callbacks = {
        button.callback_data
        for row in _support_menu_keyboard().inline_keyboard
        for button in row
        if button.callback_data
    }
    assert {
        "support:faq",
        "support:payment",
        "support:access",
        "support:contacts",
        "menu:main",
    } <= menu_callbacks
    assert (
        _support_back_keyboard().inline_keyboard[0][0].callback_data
        == "support:menu"
    )


def test_numeric_environment_contact_never_becomes_username() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        support_contacts=["808261089,@public_support"],
    )

    contacts = support_contacts_from_settings(settings)

    assert contacts[0].kind == "telegram_id"
    assert contacts[0].value == "808261089"
    assert contacts[1].kind == "username"
    assert contacts[1].value == "public_support"


@pytest.mark.parametrize("value", ["@123456", "123456", "bad-name", "abcd"])
def test_invalid_username_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_support_contact(kind="username", value=value)


@pytest.mark.asyncio
async def test_support_ids_are_resolved_to_public_username_links(
    session,
    monkeypatch,
) -> None:
    settings = Settings(_env_file=None, app_env="test", support_ids=[101, 202])
    monkeypatch.setattr(
        "app.services.support_service.get_settings",
        lambda: settings,
    )
    bot = SimpleNamespace(
        get_chat=AsyncMock(
            side_effect=[
                SimpleNamespace(username="first_support"),
                SimpleNamespace(username="second_support"),
            ]
        )
    )

    contacts = await SupportContactService(session).resolved(bot)

    assert [(item.label, item.url) for item in contacts] == [
        ("Поддержка", "https://t.me/first_support"),
        ("Поддержка", "https://t.me/second_support"),
    ]


@pytest.mark.asyncio
async def test_support_id_without_username_uses_id_link(
    session,
    monkeypatch,
) -> None:
    settings = Settings(_env_file=None, app_env="test", support_ids=[101])
    monkeypatch.setattr(
        "app.services.support_service.get_settings",
        lambda: settings,
    )
    bot = SimpleNamespace(
        get_chat=AsyncMock(return_value=SimpleNamespace(username=None))
    )

    contacts = await SupportContactService(session).resolved(bot)

    assert [(item.label, item.url) for item in contacts] == [
        ("Поддержка", "tg://user?id=101")
    ]
    assert "@101" not in str(contacts)


@pytest.mark.asyncio
async def test_runtime_support_contacts_override_and_reset_environment(
    session,
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        support_contacts=["@fallback_support"],
    )
    monkeypatch.setattr(
        "app.services.support_service.get_settings",
        lambda: settings,
    )
    service = SupportContactService(session)
    await service.save(
        [
            SupportContact("username", "first_support", "First", 0),
            SupportContact("telegram_id", "202", "Second", 1),
        ],
        actor_telegram_id=1,
    )

    runtime = await service.definitions()
    assert [(item.kind, item.value) for item in runtime] == [
        ("username", "first_support"),
        ("telegram_id", "202"),
    ]

    assert await service.reset_to_environment()
    fallback = await service.definitions()
    assert [(item.kind, item.value) for item in fallback] == [
        ("username", "fallback_support")
    ]
