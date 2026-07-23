from app.services.admin_notification_service import _format_ops_notification, _important_ops_event


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
