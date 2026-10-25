from app.core.config import get_settings
from app.payments.base import PaymentProvider
from app.payments.providers import (
    CryptoBotProvider,
    DevPaymentProvider,
    TelegramStarsProvider,
    YooKassaProvider,
)


def get_payment_providers() -> dict[str, PaymentProvider]:
    settings = get_settings()
    providers: list[PaymentProvider] = []
    if settings.enable_yookassa:
        providers.append(
            YooKassaProvider(
                settings.yookassa_shop_id,
                settings.yookassa_secret_key,
                settings.yookassa_api_url,
                str(settings.app_url),
            )
        )
    if settings.enable_cryptobot:
        providers.append(
            CryptoBotProvider(
                settings.cryptobot_token,
                settings.cryptobot_api_url,
                settings.webhook_replay_ttl_seconds,
            )
        )
    if settings.enable_telegram_stars:
        providers.append(TelegramStarsProvider(settings.telegram_stars_provider_token))
    if settings.enable_dev_payments:
        providers.insert(0, DevPaymentProvider())
    return {provider.get_provider_name(): provider for provider in providers}


def get_payment_provider(provider_name: str) -> PaymentProvider:
    providers = get_payment_providers()
    try:
        return providers[provider_name]
    except KeyError as exc:
        raise ValueError("Payment provider is disabled or unknown") from exc
