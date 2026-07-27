from types import SimpleNamespace

from app.bot.ops_runner import _allowed
from app.services.admin_notification_service import (
    _format_ops_notification,
    _important_ops_event,
)


def test_ops_notification_is_compact_and_escapes_html() -> None:
    message = _format_ops_notification("errors", "webhook.failed", "bad <payload> & retry")

    assert message.startswith("<b>PROP GATE · ALERT</b>")
    assert "<code>webhook.failed</code>" in message
    assert "bad &lt;payload&gt; &amp; retry" in message
    assert message.count("\n") == 2


def test_only_important_ops_events_are_pushed() -> None:
    assert _important_ops_event("payments", "payment.paid")
    assert _important_ops_event("errors", "webhook.failed")
    assert _important_ops_event("refunds", "payment.refunded")
    assert not _important_ops_event("subscriptions", "subscription.granted")
    assert not _important_ops_event("system", "runtime.started")


def test_ops_access_is_granted_only_to_explicit_user_ids(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.bot.ops_runner.get_settings",
        lambda: SimpleNamespace(ops_chat_ids=[101, -100777], admin_ids=[202]),
    )

    assert _allowed(101)
    assert _allowed(202)
    assert not _allowed(303)
    assert not _allowed(None)
