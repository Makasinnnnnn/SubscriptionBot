from contextlib import asynccontextmanager

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.journal.models import Account, Mutex, Slot, Trade


class Store:
    def __init__(self, url: str):
        kwargs = {}
        if url.startswith("postgresql+asyncpg"):
            kwargs["connect_args"] = {"server_settings": {"timezone": "UTC"}}
        elif url.startswith("sqlite"):
            kwargs["connect_args"] = {"timeout": 30}
        self.engine = create_async_engine(url, pool_pre_ping=True, **kwargs)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def locked(self):
        """Serialize ID allocation, drafts, and the low-volume journal sync worker.

        Uses database locks, so another process cannot allocate the same ID.
        No threading/process-local lock is relied on for correctness.
        """
        async with self.sessions() as session, session.begin():
            if self.engine.dialect.name == "sqlite":
                await session.execute(text("BEGIN IMMEDIATE"))
            mutex = await session.scalar(select(Mutex).where(Mutex.id == 1).with_for_update())
            if mutex is None:
                raise RuntimeError("Journal migration required: python -m app.journal.migrate")
            yield session

    async def bootstrap(self, accounts: list[dict], slots: list[dict]):
        async with self.locked() as session:
            for data in accounts:
                account = await session.get(Account, data["id"])
                if account:
                    account.payload = data
                else:
                    session.add(Account(id=data["id"], payload=data))
            for data in slots:
                slot = await session.get(Slot, data["id"])
                if await session.get(Trade, data["id"]):
                    continue
                if slot:
                    slot.blocked = data["blocked"]
                    slot.baseline = data["baseline"]
                else:
                    session.add(Slot(**data))

    async def close(self):
        await self.engine.dispose()
