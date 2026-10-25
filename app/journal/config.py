from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class JournalSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JOURNAL_", env_file=".env.journal", extra="ignore"
    )

    bot_token: SecretStr = SecretStr("")
    database_url: str = "sqlite+aiosqlite:///journal.db"
    admin_ids: list[int] = Field(default_factory=list)
    # Empty mapping gives no traders access. Admins have access to every imported account.
    trader_accounts: dict[str, list[str]] = Field(default_factory=dict)
    spreadsheet_id: str = "1hTq6mZUNivnEsxMi5B4hjDuDhoh7WcQVJ3JRqkiChE8"
    screenshots_folder_id: str = "1Kddiu34NN-hOC3bygiNw02b4MOiBCkzF"
    trades_sheet: str = "Сделки"
    screenshots_sheet: str = "Скриншоты"
    accounts_sheet: str = "Счета"
    timezone: str = "Asia/Tomsk"
    google_credentials: Path = Path.home() / ".config/propgate-journal/google.json"
    google_client_secret: Path = Path.home() / ".config/propgate-journal/client.json"
    max_attempts: int = Field(default=6, ge=1, le=20)
    sync_interval: float = Field(default=5, ge=1)
    max_image_bytes: int = Field(default=15_000_000, ge=1024)

    @field_validator("admin_ids")
    @classmethod
    def positive_ids(cls, value: list[int]) -> list[int]:
        if any(i <= 0 for i in value):
            raise ValueError("Telegram user IDs must be positive")
        return value

    def allowed_accounts(self, user_id: int) -> list[str] | None:
        if user_id in self.admin_ids:
            return None  # admin: all accounts
        return self.trader_accounts.get(str(user_id), [])

    def authorized(self, user_id: int) -> bool:
        return user_id in self.admin_ids or bool(self.trader_accounts.get(str(user_id)))
