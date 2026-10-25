import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import session as database


@pytest.mark.asyncio
async def test_postgres_application_timestamps_use_utc_on_non_utc_server():
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required")
    control = create_async_engine(url)
    role = "timezone_" + uuid4().hex
    password = uuid4().hex
    try:
        async with control.begin() as connection:
            await connection.execute(text(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}'"))
            await connection.execute(text(f"ALTER ROLE \"{role}\" SET timezone = 'Asia/Tomsk'"))
        application_url = make_url(url).set(username=role, password=password)
        application = database.create_database_engine(application_url)
        try:
            async with application.begin() as connection:
                timezone = await connection.scalar(text("SHOW timezone"))
                stored = await connection.scalar(text("SELECT now()::timestamp"))
                real_creation = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=24)
                stored_creation = stored - timedelta(hours=24)
                window_closed = stored_creation <= datetime.now(UTC).replace(
                    tzinfo=None
                ) - timedelta(hours=23)
                assert timezone == "UTC", timezone
                assert abs((stored - datetime.now(UTC).replace(tzinfo=None)).total_seconds()) < 5
                assert window_closed and real_creation < stored_creation + timedelta(seconds=5)
        finally:
            await application.dispose()
    finally:
        async with control.begin() as connection:
            await connection.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        await control.dispose()
