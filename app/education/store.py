from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.education import EducationPreference


async def save_preferences(session: AsyncSession, telegram_id: int, values: dict) -> None:
    # Explicit allowlist: no balances, positions or private trading credentials.
    saved = {
        key: values[key]
        for key in ("account_type", "nominal", "risk_percent", "leverage")
        if key in values
    }
    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    stmt = insert(EducationPreference).values(telegram_id=telegram_id, values=saved)
    await session.execute(
        stmt.on_conflict_do_update(index_elements=["telegram_id"], set_={"values": saved})
    )
