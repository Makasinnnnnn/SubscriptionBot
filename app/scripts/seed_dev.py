import asyncio
from decimal import Decimal

from sqlalchemy import select

from app.admin.service import AdminPermissionService
from app.core.config import get_settings
from app.db.models import Channel, Plan
from app.db.session import async_session_factory


async def seed_dev() -> None:
    settings = get_settings()
    if settings.dev_channel_id is None and settings.dev_admin_id is None:
        print("Set DEV_CHANNEL_ID and/or DEV_ADMIN_ID before running the seed command.")
        return

    async with async_session_factory() as session:
        if settings.dev_admin_id is not None:
            await AdminPermissionService(session).add_admin(settings.dev_admin_id)

        if settings.dev_channel_id is not None:
            channel = await session.scalar(
                select(Channel).where(Channel.telegram_chat_id == settings.dev_channel_id)
            )
            if channel is None:
                channel = Channel(
                    telegram_chat_id=settings.dev_channel_id,
                    title="PropGate Dev",
                    description="Development channel",
                )
                session.add(channel)
                await session.flush()
            plan = await session.scalar(
                select(Plan).where(
                    Plan.channel_id == channel.id,
                    Plan.title == "Dev 30 days",
                )
            )
            if plan is None:
                session.add(
                    Plan(
                        channel_id=channel.id,
                        title="Dev 30 days",
                        duration_days=30,
                        price=Decimal(100),
                        currency="RUB",
                    )
                )
        await session.commit()
    print("Development seed completed.")


if __name__ == "__main__":
    asyncio.run(seed_dev())
