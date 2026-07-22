import logging

from app.core.logging import RedactingFormatter


def test_log_formatter_redacts_tokens_secrets_and_webhook_paths() -> None:
    secret = "super-secret-value"
    token = "123456789:" + "a" * 35
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=(
            f"secret={secret} token={token} "
            "path=/webhooks/cryptobot/private-path-value"
        ),
        args=(),
        exc_info=None,
    )
    record.request_id = "test"
    formatter = RedactingFormatter("%(message)s", [secret])

    rendered = formatter.format(record)

    assert secret not in rendered
    assert token not in rendered
    assert "private-path-value" not in rendered
    assert rendered.count("***") == 3
