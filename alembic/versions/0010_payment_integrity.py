from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision = "0010_payment_integrity"
down_revision = "0009_discount_promos"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("payments") as batch:
        batch.add_column(sa.Column("purchased_duration_days", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("purchased_channel_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_payment_purchased_channel",
            "channels",
            ["purchased_channel_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.alter_column("amount", existing_type=sa.Numeric(12, 2), type_=sa.Numeric(38, 18))
    with op.batch_alter_table("plans") as batch:
        batch.alter_column("price", existing_type=sa.Numeric(12, 2), type_=sa.Numeric(38, 18))
    op.create_table(
        "payment_applications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "payment_id",
            sa.Integer(),
            sa.ForeignKey("payments.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("subscriptions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("days_granted", sa.Integer(), nullable=False),
        sa.Column("applied_at", sa.DateTime(), nullable=False),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("reversed_at", sa.DateTime(), nullable=True),
        sa.Column("legacy", sa.Boolean(), nullable=False),
    )
    op.create_index(
        "ix_payment_applications_subscription_id", "payment_applications", ["subscription_id"]
    )
    op.create_table(
        "refunds",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "payment_id",
            sa.Integer(),
            sa.ForeignKey("payments.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("amount", sa.Numeric(38, 18), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False, unique=True),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    if op.get_context().as_sql:
        op.execute("""
            UPDATE payments p SET purchased_duration_days = plan.duration_days,
                purchased_channel_id = plan.channel_id,
                metadata_json = COALESCE(p.metadata_json::jsonb, '{}'::jsonb)
                    || jsonb_build_object('terms_origin', 'migration_catalog_unverified')
            FROM plans plan WHERE plan.id = p.plan_id
        """)
        op.execute("""
            INSERT INTO payment_applications
                (payment_id, subscription_id, days_granted, applied_at, starts_at, expires_at, legacy)
            SELECT DISTINCT ON (p.id) p.id, s.id, 0,
                COALESCE(p.paid_at, CURRENT_TIMESTAMP AT TIME ZONE 'UTC'),
                s.starts_at, s.expires_at, TRUE
            FROM payments p JOIN plans plan ON plan.id = p.plan_id
                JOIN subscriptions s ON s.user_id = p.user_id AND s.channel_id = plan.channel_id
            WHERE p.status = 'paid' AND s.payment_id IS NOT NULL AND p.id <= s.payment_id
            ORDER BY p.id, s.id DESC
        """)
        return
    connection = op.get_bind()
    metadata = sa.MetaData()
    payments = sa.Table("payments", metadata, autoload_with=connection)
    plans = sa.Table("plans", metadata, autoload_with=connection)
    subscriptions = sa.Table("subscriptions", metadata, autoload_with=connection)
    applications = sa.Table("payment_applications", metadata, autoload_with=connection)
    rows = (
        connection.execute(
            sa.select(payments, plans.c.duration_days, plans.c.channel_id).join(
                plans, plans.c.id == payments.c.plan_id
            )
        )
        .mappings()
        .all()
    )
    existing_subscriptions = connection.execute(sa.select(subscriptions)).mappings().all()
    for payment in rows:
        terms = dict(payment["metadata_json"] or {})
        terms["terms_origin"] = "migration_catalog_unverified"
        connection.execute(
            payments.update()
            .where(payments.c.id == payment["id"])
            .values(
                purchased_duration_days=payment["duration_days"],
                purchased_channel_id=payment["channel_id"],
                metadata_json=terms,
            )
        )
        if str(payment["status"]) != "paid":
            continue
        candidates = [
            sub
            for sub in existing_subscriptions
            if sub["user_id"] == payment["user_id"]
            and sub["channel_id"] == payment["channel_id"]
            and sub["payment_id"] is not None
            and payment["id"] <= sub["payment_id"]
        ]
        if not candidates:
            continue
        sub = max(candidates, key=lambda item: item["id"])
        connection.execute(
            applications.insert().values(
                payment_id=payment["id"],
                subscription_id=sub["id"],
                days_granted=0,
                applied_at=payment["paid_at"] or datetime.now(UTC).replace(tzinfo=None),
                starts_at=sub["starts_at"],
                expires_at=sub["expires_at"],
                legacy=True,
            )
        )


def downgrade() -> None:
    op.drop_table("refunds")
    op.drop_table("payment_applications")
    with op.batch_alter_table("payments") as batch:
        batch.drop_constraint("fk_payment_purchased_channel", type_="foreignkey")
        batch.drop_column("purchased_channel_id")
        batch.drop_column("purchased_duration_days")
        batch.alter_column("amount", existing_type=sa.Numeric(38, 18), type_=sa.Numeric(12, 2))
    with op.batch_alter_table("plans") as batch:
        batch.alter_column("price", existing_type=sa.Numeric(38, 18), type_=sa.Numeric(12, 2))
