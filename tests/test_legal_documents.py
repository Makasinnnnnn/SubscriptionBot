from app.bot.handlers.legal import DOCUMENTS, _document_pages, _plain_markdown
from app.bot.keyboards.subscription import legal_acceptance_keyboard, payment_providers_keyboard
from app.db.models import Plan


def test_bundled_legal_documents_are_available_and_paginated() -> None:
    for key, (_, path) in DOCUMENTS.items():
        assert path.is_file(), key
        pages = _document_pages(key)
        assert pages
        assert all(len(page) <= 3000 for page in pages)


def test_legal_payment_keyboard_uses_internal_tabs() -> None:
    keyboard = legal_acceptance_keyboard(12, "telegram-stars")
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert "legaldoc:terms:0:12:telegram-stars" in callbacks
    assert "legaldoc:privacy:0:12:telegram-stars" in callbacks
    assert "legaldoc:refunds:0:12:telegram-stars" in callbacks
    assert all(callback is None or len(callback.encode()) <= 64 for callback in callbacks)


def test_discount_promo_survives_legal_and_provider_callbacks() -> None:
    legal_keyboard = legal_acceptance_keyboard(
        12,
        "telegram-stars",
        promo_id=34,
    )
    legal_callbacks = [
        button.callback_data for row in legal_keyboard.inline_keyboard for button in row
    ]

    assert "legaldoc:terms:0:12:telegram-stars:34" in legal_callbacks
    assert "legalaccept:12:telegram-stars:34" in legal_callbacks
    assert all(callback is None or len(callback.encode()) <= 64 for callback in legal_callbacks)


def test_markdown_heading_is_rendered_as_plain_text() -> None:
    assert _plain_markdown("# Heading\nBody") == "Heading\nBody"


def test_ruble_plan_offers_crypto_and_configured_stars(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.bot.keyboards.subscription.get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "enable_dev_payments": False,
                "enable_yookassa": False,
                "yookassa_shop_id": "",
                "yookassa_secret_key": "",
                "enable_cryptobot": True,
                "cryptobot_token": "configured",
                "enable_telegram_stars": True,
            },
        )(),
    )
    plan = Plan(
        id=1,
        channel_id=1,
        title="Month",
        duration_days=30,
        price=4990,
        currency="RUB",
        stars_price=500,
    )
    callbacks = [
        button.callback_data
        for row in payment_providers_keyboard(plan).inline_keyboard
        for button in row
    ]
    assert "pay:1:telegram-stars" in callbacks
    assert "pay:1:cryptobot" in callbacks

    promo_callbacks = [
        button.callback_data
        for row in payment_providers_keyboard(plan, promo_id=34).inline_keyboard
        for button in row
    ]
    assert "pay:1:telegram-stars:34" in promo_callbacks
    assert "pay:1:cryptobot:34" in promo_callbacks
