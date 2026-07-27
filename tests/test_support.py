from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.admin.settings_service import RuntimeSettingsService
from app.bot.handlers.legal import (
    _support_back_keyboard,
    _support_keyboard,
    _support_menu_keyboard,
)
from app.core.config import Settings
from app.services.support_service import (
    SupportContact,
    SupportContactService,
    ResolvedSupportContact,
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


def test_support_keyboard_has_unique_links_and_one_relay_action() -> None:
    keyboard = _support_keyboard(
        [
            ResolvedSupportContact(
                label="Support ID",
                url="https://t.me/public_support",
                source=SupportContact(
                    kind="telegram_id",
                    value="101",
                    label="Support ID",
                ),
            ),
            ResolvedSupportContact(
                label="@public_support",
                url="https://t.me/public_support",
                source=SupportContact(
                    kind="username",
                    value="public_support",
                    label="@public_support",
                ),
            ),
        ],
        back_callback="support:menu",
    )

    assert keyboard.inline_keyboard[0][0].url == "https://t.me/public_support"
    assert keyboard.inline_keyboard[1][0].text == "Оставить обращение в поддержку"
    assert keyboard.inline_keyboard[1][0].callback_data == "support:write"


def test_support_keyboard_without_telegram_id_has_no_relay_action() -> None:
    keyboard = _support_keyboard(
        [
            ResolvedSupportContact(
                label="@public_support",
                url="https://t.me/public_support",
                source=SupportContact(
                    kind="username",
                    value="public_support",
                    label="@public_support",
                ),
            )
        ],
        back_callback="support:menu",
    )

    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert "support:write" not in callbacks


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
async def test_support_id_and_matching_username_are_resolved_once(
    session,
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        support_ids=[101],
        support_contacts=["@public_support"],
    )
    monkeypatch.setattr(
        "app.services.support_service.get_settings",
        lambda: settings,
    )
    bot = SimpleNamespace(
        get_chat=AsyncMock(
            return_value=SimpleNamespace(username="public_support")
        )
    )

    contacts = await SupportContactService(session).resolved(bot)

    assert len(contacts) == 1
    assert contacts[0].url == "https://t.me/public_support"
    assert contacts[0].source.kind == "telegram_id"


@pytest.mark.asyncio
async def test_matching_contact_keeps_relay_capability_regardless_of_order(
    session,
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        support_contacts=["@public_support", "101"],
    )
    monkeypatch.setattr(
        "app.services.support_service.get_settings",
        lambda: settings,
    )
    bot = SimpleNamespace(
        get_chat=AsyncMock(
            return_value=SimpleNamespace(username="public_support")
        )
    )

    contacts = await SupportContactService(session).resolved(bot)

    assert len(contacts) == 1
    assert contacts[0].source.kind == "telegram_id"


@pytest.mark.asyncio
async def test_relay_target_prefers_first_telegram_id(
    session,
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        support_contacts=["@public_support", "101", "202"],
    )
    monkeypatch.setattr(
        "app.services.support_service.get_settings",
        lambda: settings,
    )

    target = await SupportContactService(session).relay_target()

    assert target is not None
    assert target.kind == "telegram_id"
    assert target.value == "101"


@pytest.mark.asyncio
async def test_support_id_without_username_uses_bot_relay(
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
        ("Поддержка", None)
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


@pytest.mark.asyncio
async def test_empty_runtime_support_falls_back_to_environment(
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

    with pytest.raises(ValueError, match="хотя бы один"):
        await service.save([], actor_telegram_id=1)

    await RuntimeSettingsService(session).set(
        RuntimeSettingsService.SUPPORT_CONTACTS,
        [],
        actor_telegram_id=1,
    )
    contacts = await service.definitions()

    assert [(item.kind, item.value) for item in contacts] == [
        ("username", "fallback_support")
    ]
