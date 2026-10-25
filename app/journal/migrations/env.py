import asyncio

from alembic import context
from app.journal.config import JournalSettings
from app.journal.models import Base
from app.journal.store import Store


def migrate(connection):
    context.configure(connection=connection, target_metadata=Base.metadata,
                      version_table="journal_alembic_version")
    with context.begin_transaction():
        context.run_migrations()


async def online():
    store = Store(JournalSettings().database_url)
    try:
        async with store.engine.connect() as connection:
            await connection.run_sync(migrate)
    finally:
        await store.close()


if context.is_offline_mode():
    context.configure(url=JournalSettings().database_url, target_metadata=Base.metadata,
                      version_table="journal_alembic_version", literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
