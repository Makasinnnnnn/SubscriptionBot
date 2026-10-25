from datetime import datetime, timedelta, timezone

from app.subscriptions.presentation import access_delivery_text


def test_access_delivery_contains_both_links_in_one_message() -> None:
    text = access_delivery_text(
        expires_at=datetime(2026, 8, 23, 12, 30),
        primary_link="https://t.me/+primary",
        materials_link="https://t.me/+materials",
    )

    assert "Доступ активирован до 23.08.2026 12:30" in text
    assert "Основной канал:\nhttps://t.me/+primary" in text
    assert "Методички:\nhttps://t.me/+materials" in text
    assert "подтвердит её автоматически" in text


def test_access_delivery_normalizes_timezone() -> None:
    text = access_delivery_text(
        expires_at=datetime(2026, 8, 23, 19, 30, tzinfo=timezone(timedelta(hours=7))),
        primary_link="https://t.me/+primary",
        materials_link=None,
    )

    assert "23.08.2026 12:30 UTC" in text
