from collections.abc import AsyncGenerator

from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


def create_database_engine(database_url: str | URL) -> AsyncEngine:
    url = make_url(database_url)
    connect_args = {}
    if url.get_backend_name() == "postgresql":
        if url.get_driver_name() == "asyncpg":
            connect_args = {"server_settings": {"timezone": "UTC"}}
        elif url.get_driver_name() in {"psycopg", "psycopg_async"}:
            connect_args = {"options": "-c timezone=UTC"}
    return create_async_engine(url, pool_pre_ping=True, connect_args=connect_args)


settings = get_settings()
engine = create_database_engine(settings.database_url)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session
