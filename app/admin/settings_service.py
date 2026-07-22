from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppSetting


class RuntimeSettingsService:
    NOTIFICATION_DAYS = "notification_days"
    INVITE_TTL_MINUTES = "invite_ttl_minutes"
    SCHEDULER_INTERVAL_MINUTES = "scheduler_interval_minutes"

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, key: str, default: Any) -> Any:
        setting = await self.session.scalar(select(AppSetting).where(AppSetting.key == key))
        if setting is None:
            return default
        return setting.value_json.get("value", default)

    async def set(self, key: str, value: Any, actor_telegram_id: int | None) -> AppSetting:
        setting = await self.session.scalar(select(AppSetting).where(AppSetting.key == key))
        if setting is None:
            setting = AppSetting(key=key)
            self.session.add(setting)
        setting.value_json = {"value": value}
        setting.updated_by_telegram_id = actor_telegram_id
        await self.session.flush()
        return setting

    async def notification_days(self, default: list[int]) -> list[int]:
        value = await self.get(self.NOTIFICATION_DAYS, default)
        return sorted({int(day) for day in value if 0 < int(day) <= 365}, reverse=True)

    async def invite_ttl_minutes(self, default: int = 60) -> int:
        return max(5, min(10080, int(await self.get(self.INVITE_TTL_MINUTES, default))))

    async def scheduler_interval_minutes(self, default: int = 10) -> int:
        return max(
            1,
            min(1440, int(await self.get(self.SCHEDULER_INTERVAL_MINUTES, default))),
        )
