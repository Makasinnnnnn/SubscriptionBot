from app.services.admin_notification_service import _format_ops_notification


def test_ops_notification_is_compact_and_escapes_html() -> None:
    message = _format_ops_notification("errors", "webhook.failed", "bad <payload> & retry")

    assert message.startswith("<b>PROP GATE · ALERT</b>")
    assert "<code>webhook.failed</code>" in message
    assert "bad &lt;payload&gt; &amp; retry" in message
    assert message.count("\n") == 2
