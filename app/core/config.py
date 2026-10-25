import ipaddress
import re
from functools import lru_cache

from pydantic import AnyHttpUrl, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    app_name: str = "SubscriptionBot"
    app_env: str = "development"
    app_url: AnyHttpUrl = "http://localhost:8000"
    app_mode: str = "api"
    debug: bool = False
    log_level: str = "INFO"
    allowed_hosts: list[str] = Field(default_factory=lambda: ["*"])

    bot_token: str = Field(default="", repr=False)
    ops_bot_token: str = Field(default="", repr=False)
    ops_chat_ids: list[int] = Field(default_factory=list)
    ops_api_url: str = "http://app:8000"
    admin_ids: list[int] = Field(default_factory=list)
    admin_username: str = "admin"
    admin_password: str = Field(default="", repr=False)
    enable_dev_payments: bool = True
    enable_telegram_stars: bool = True
    enable_cryptobot: bool = False
    enable_yookassa: bool = False

    database_url: str = Field(
        default="postgresql+asyncpg://subscription:subscription@postgres:5432/subscription",
        repr=False,
    )

    yookassa_shop_id: str = ""
    yookassa_secret_key: str = Field(default="", repr=False)
    yookassa_api_url: str = "https://api.yookassa.ru/v3"
    cryptobot_token: str = Field(default="", repr=False)
    cryptobot_api_url: str = "https://pay.crypt.bot/api"
    telegram_stars_provider_token: str = Field(default="", repr=False)
    webhook_secret: str = Field(default="", repr=False)
    webhook_max_body_bytes: int = Field(default=1_000_000, ge=1, le=10_000_000)
    webhook_replay_ttl_seconds: int = Field(default=259_200, ge=60, le=604_800)
    yookassa_allowed_ips: list[str] = Field(default_factory=list)
    trusted_proxy_ips: list[str] = Field(default_factory=list)

    notification_days: list[int] = Field(default_factory=lambda: [3, 1])
    notification_intervals_minutes: list[int] = Field(
        default_factory=lambda: [10080, 4320, 1440, 720]
    )
    scheduler_interval_minutes: int = Field(default=10, ge=1, le=1440)
    dev_channel_id: int | None = None
    dev_admin_id: int | None = None
    terms_url: str = ""
    privacy_url: str = ""
    refund_policy_url: str = ""
    legal_version: str = "1"
    support_contact: str = ""
    support_contacts: list[str] = Field(default_factory=list)
    support_ids: list[int] = Field(default_factory=list)
    materials_channel_id: int | None = None
    materials_channel_title: str = "Методички"
    enable_builtin_education: bool = False
    bot_description: str = "PropGate - оплата подписки и управление доступом к закрытому каналу."
    bot_short_description: str = "Подписка и доступ к закрытому каналу PropGate."
    backup_retention_count: int = Field(default=14, ge=1, le=365)

    @model_validator(mode="after")
    def validate_environment(self) -> "Settings":
        if self.app_env not in {"development", "test", "production"}:
            raise ValueError("APP_ENV must be development, test, or production")
        if self.enable_cryptobot and not self.cryptobot_token:
            raise ValueError("CRYPTOBOT_TOKEN is required when ENABLE_CRYPTOBOT=true")
        if self.enable_yookassa and not (self.yookassa_shop_id and self.yookassa_secret_key):
            raise ValueError("YooKassa credentials are required when ENABLE_YOOKASSA=true")
        if any(day <= 0 or day > 365 for day in self.notification_days):
            raise ValueError("NOTIFICATION_DAYS values must be between 1 and 365")
        if (
            not self.notification_intervals_minutes
            or any(value <= 0 or value > 525_600 for value in self.notification_intervals_minutes)
            or len(set(self.notification_intervals_minutes))
            != len(self.notification_intervals_minutes)
            or len(self.notification_intervals_minutes) > 12
        ):
            raise ValueError(
                "NOTIFICATION_INTERVALS_MINUTES must contain 1-12 unique values "
                "between 1 and 525600"
            )
        if self.app_env != "production":
            return self

        errors: list[str] = []
        placeholders = ("change-me", "changeme", "replace-with", "example")
        if self.debug:
            errors.append("DEBUG must be false")
        if self.enable_dev_payments:
            errors.append("ENABLE_DEV_PAYMENTS must be false")
        if not self.bot_token or any(item in self.bot_token.lower() for item in placeholders):
            errors.append("BOT_TOKEN must be configured with a non-placeholder value")
        if not self.admin_ids:
            errors.append("ADMIN_IDS must contain at least one superadmin")
        if self.materials_channel_id is None:
            errors.append("MATERIALS_CHANNEL_ID is required for the second paid channel")
        if len(self.webhook_secret) < 32 or any(
            item in self.webhook_secret.lower() for item in placeholders
        ):
            errors.append(
                "WEBHOOK_SECRET must be a non-placeholder value of at least 32 characters"
            )
        elif not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", self.webhook_secret):
            errors.append("WEBHOOK_SECRET must be URL-safe and at most 128 characters")
        if (
            len(self.admin_password) < 16
            or self.admin_password.lower()
            in {"admin", "password", "changeme", "replace-with-a-long-password"}
            or any(item in self.admin_password.lower() for item in placeholders)
        ):
            errors.append("ADMIN_PASSWORD must be a non-default value of at least 16 characters")
        database_url = self.database_url.lower()
        if not database_url.startswith("postgresql+"):
            errors.append("DATABASE_URL must use PostgreSQL in production")
        if "subscription:subscription@" in database_url or any(
            item in database_url for item in placeholders
        ):
            errors.append("DATABASE_URL must not use default or placeholder credentials")
        if not self.allowed_hosts or "*" in self.allowed_hosts:
            errors.append("ALLOWED_HOSTS must contain explicit host names")
        if any("/" in host or "://" in host for host in self.allowed_hosts):
            errors.append("ALLOWED_HOSTS entries must be host names without scheme or path")
        app_host = self.app_url.host
        if app_host and app_host not in self.allowed_hosts:
            errors.append("ALLOWED_HOSTS must include the APP_URL host")
        if bool(self.ops_bot_token) != bool(self.ops_chat_ids):
            errors.append("OPS_BOT_TOKEN and OPS_CHAT_IDS must be configured together")
        if self.enable_cryptobot and (
            not self.cryptobot_api_url.startswith("https://")
            or any(item in self.cryptobot_token.lower() for item in placeholders)
        ):
            errors.append("Crypto Pay must use HTTPS and non-placeholder credentials")
        if self.enable_yookassa and (
            not self.yookassa_api_url.startswith("https://")
            or any(item in self.yookassa_secret_key.lower() for item in placeholders)
        ):
            errors.append("YooKassa must use HTTPS and non-placeholder credentials")
        for value in [*self.trusted_proxy_ips, *self.yookassa_allowed_ips]:
            try:
                ipaddress.ip_network(value, strict=False)
            except ValueError:
                errors.append(
                    "TRUSTED_PROXY_IPS and YOOKASSA_ALLOWED_IPS must contain valid networks"
                )
                break
        for name, value in {
            "TERMS_URL": self.terms_url,
            "PRIVACY_URL": self.privacy_url,
            "REFUND_POLICY_URL": self.refund_policy_url,
        }.items():
            if value and not value.startswith("https://"):
                errors.append(f"{name} must use HTTPS when configured")
        if not self.support_ids and not self.support_contact and not self.support_contacts:
            errors.append("SUPPORT_IDS is required")
        if str(self.app_url).startswith("http://"):
            errors.append("APP_URL must use HTTPS")
        if errors:
            raise ValueError("Invalid production configuration: " + "; ".join(errors))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
