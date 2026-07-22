import logging
import re

from app.core.config import get_settings
from app.core.request_context import request_id_context


class RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_context.get()
        return True


class RedactingFormatter(logging.Formatter):
    _patterns = (
        (re.compile(r"(?i)(authorization:\s*(?:bearer|basic)\s+)[^\s]+"), r"\1***"),
        (re.compile(r"(?i)(/webhooks/(?:cryptobot|yookassa)/)[^/?\s]+"), r"\1***"),
        (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"), "***"),
    )

    def __init__(self, fmt: str, secrets: list[str]) -> None:
        super().__init__(fmt)
        self._secrets = sorted(
            {value for value in secrets if value and len(value) >= 8},
            key=len,
            reverse=True,
        )

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        for value in self._secrets:
            rendered = rendered.replace(value, "***")
        for pattern, replacement in self._patterns:
            rendered = pattern.sub(replacement, rendered)
        return rendered


def setup_logging() -> None:
    settings = get_settings()
    log_format = (
        "%(asctime)s %(levelname)s [%(name)s] "
        "request_id=%(request_id)s %(message)s"
    )
    logging.basicConfig(
        level=settings.log_level.upper(),
        format=log_format,
        force=True,
    )
    context_filter = RequestContextFilter()
    formatter = RedactingFormatter(
        log_format,
        [
            settings.bot_token,
            settings.ops_bot_token,
            settings.admin_password,
            settings.webhook_secret,
            settings.cryptobot_token,
            settings.yookassa_secret_key,
        ],
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(context_filter)
        handler.setFormatter(formatter)
