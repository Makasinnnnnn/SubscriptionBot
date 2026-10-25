import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_payment_integrity_migration_preserves_and_flags_legacy_history():
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table("users", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("channels", metadata, sa.Column("id", sa.Integer(), primary_key=True))
    plans = sa.Table(
        "plans",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("channel_id", sa.Integer()),
        sa.Column("duration_days", sa.Integer()),
        sa.Column("price", sa.Numeric(12, 2)),
    )
    payments = sa.Table(
        "payments",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer()),
        sa.Column("plan_id", sa.Integer()),
        sa.Column("amount", sa.Numeric(12, 2)),
        sa.Column("status", sa.String()),
        sa.Column("paid_at", sa.DateTime()),
        sa.Column("metadata_json", sa.JSON()),
    )
    subscriptions = sa.Table(
        "subscriptions",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer()),
        sa.Column("channel_id", sa.Integer()),
        sa.Column("payment_id", sa.Integer()),
        sa.Column("starts_at", sa.DateTime()),
        sa.Column("expires_at", sa.DateTime()),
    )
    metadata.create_all(engine)
    path = (
        Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0010_payment_integrity.py"
    )
    spec = importlib.util.spec_from_file_location("payment_integrity_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    now = datetime(2026, 10, 13)
    with engine.begin() as connection:
        connection.execute(metadata.tables["users"].insert().values(id=1))
        connection.execute(metadata.tables["channels"].insert().values(id=1))
        connection.execute(plans.insert().values(id=1, channel_id=1, duration_days=3, price=10))
        connection.execute(
            payments.insert(),
            [
                {
                    "id": 1,
                    "user_id": 1,
                    "plan_id": 1,
                    "amount": 10,
                    "status": "paid",
                    "paid_at": now,
                    "metadata_json": {},
                },
                {
                    "id": 2,
                    "user_id": 1,
                    "plan_id": 1,
                    "amount": 10,
                    "status": "paid",
                    "paid_at": now,
                    "metadata_json": {},
                },
                {
                    "id": 3,
                    "user_id": 1,
                    "plan_id": 1,
                    "amount": 10,
                    "status": "pending",
                    "paid_at": None,
                    "metadata_json": {},
                },
            ],
        )
        connection.execute(
            subscriptions.insert().values(
                id=1,
                user_id=1,
                channel_id=1,
                payment_id=2,
                starts_at=now,
                expires_at=now + timedelta(days=65),
            )
        )
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
        upgraded = sa.MetaData()
        upgraded.reflect(bind=connection)
        applications = (
            connection.execute(sa.select(upgraded.tables["payment_applications"])).mappings().all()
        )
        assert {row["payment_id"] for row in applications} == {1, 2}
        assert all(row["legacy"] and row["days_granted"] == 0 for row in applications)
        stored = connection.execute(sa.select(upgraded.tables["payments"])).mappings().all()
        assert len(stored) == 3
        assert all(
            row["metadata_json"]["terms_origin"] == "migration_catalog_unverified" for row in stored
        )
        assert connection.scalar(
            sa.select(upgraded.tables["subscriptions"].c.expires_at)
        ) == now + timedelta(days=65)
    engine.dispose()
