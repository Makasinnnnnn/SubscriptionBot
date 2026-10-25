"""Add admin, audit, notifications, and access lifecycle fields.

Revision ID: 0002_admin_access_lifecycle
Revises: 0001_initial
Create Date: 2026-07-22
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002_admin_access_lifecycle"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    dialect_name = op.get_context().dialect.name
    json_type = postgresql.JSONB() if dialect_name == "postgresql" else sa.JSON()
    json_default = sa.text("'{}'::jsonb") if dialect_name == "postgresql" else sa.text("'{}'")

    op.add_column(
        "plans",
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
    )

    op.add_column(
        "payments",
        sa.Column("provider_payload", json_type, server_default=json_default, nullable=False),
    )
    op.add_column("payments", sa.Column("paid_at", sa.DateTime(), nullable=True))
    op.add_column("payments", sa.Column("failed_at", sa.DateTime(), nullable=True))
    op.add_column("payments", sa.Column("refunded_at", sa.DateTime(), nullable=True))

    if dialect_name == "sqlite":
        with op.batch_alter_table("subscriptions") as batch_op:
            batch_op.alter_column("payment_id", existing_type=sa.Integer(), nullable=True)
            batch_op.add_column(sa.Column("revoked_at", sa.DateTime(), nullable=True))
            batch_op.add_column(sa.Column("revoke_reason", sa.String(length=512), nullable=True))
            batch_op.add_column(
                sa.Column("source", sa.String(length=32), server_default="payment", nullable=False)
            )
            batch_op.create_index("ix_subscriptions_source", ["source"])
    else:
        op.alter_column("subscriptions", "payment_id", existing_type=sa.Integer(), nullable=True)
        op.add_column("subscriptions", sa.Column("revoked_at", sa.DateTime(), nullable=True))
        op.add_column(
            "subscriptions",
            sa.Column("revoke_reason", sa.String(length=512), nullable=True),
        )
        op.add_column(
            "subscriptions",
            sa.Column("source", sa.String(length=32), server_default="payment", nullable=False),
        )
        op.create_index("ix_subscriptions_source", "subscriptions", ["source"])

    op.add_column(
        "invite_links",
        sa.Column("provider_invite_link_id", sa.String(length=255), nullable=True),
    )
    op.add_column("invite_links", sa.Column("revoked_at", sa.DateTime(), nullable=True))

    op.create_table(
        "admins",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=32), server_default="admin", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("all_channels", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("telegram_id"),
    )
    op.create_index("ix_admins_telegram_id", "admins", ["telegram_id"])
    op.create_index("ix_admins_is_active", "admins", ["is_active"])

    op.create_table(
        "admin_channel_access",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "admin_id",
            sa.Integer(),
            sa.ForeignKey("admins.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "channel_id",
            sa.Integer(),
            sa.ForeignKey("channels.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("can_manage", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("admin_id", "channel_id", name="uq_admin_channel_access"),
    )
    op.create_index("ix_admin_channel_access_admin_id", "admin_channel_access", ["admin_id"])
    op.create_index("ix_admin_channel_access_channel_id", "admin_channel_access", ["channel_id"])

    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value_json", json_type, server_default=json_default, nullable=False),
        sa.Column("updated_by_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("key"),
    )
    op.create_index("ix_app_settings_key", "app_settings", ["key"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=255), nullable=True),
        sa.Column("metadata_json", json_type, server_default=json_default, nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_logs_actor_telegram_id", "audit_logs", ["actor_telegram_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_target_type", "audit_logs", ["target_type"])

    op.create_table(
        "notification_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "subscription_id",
            "type",
            name="uq_notification_subscription_type",
        ),
    )
    op.create_index("ix_notification_logs_user_id", "notification_logs", ["user_id"])
    op.create_index(
        "ix_notification_logs_subscription_id",
        "notification_logs",
        ["subscription_id"],
    )
    op.create_index("ix_notification_logs_type", "notification_logs", ["type"])


def downgrade() -> None:
    dialect_name = op.get_context().dialect.name
    op.drop_table("notification_logs")
    op.drop_table("audit_logs")
    op.drop_table("app_settings")
    op.drop_table("admin_channel_access")
    op.drop_table("admins")

    op.drop_column("invite_links", "revoked_at")
    op.drop_column("invite_links", "provider_invite_link_id")

    if dialect_name == "sqlite":
        with op.batch_alter_table("subscriptions") as batch_op:
            batch_op.drop_index("ix_subscriptions_source")
            batch_op.drop_column("source")
            batch_op.drop_column("revoke_reason")
            batch_op.drop_column("revoked_at")
            batch_op.alter_column("payment_id", existing_type=sa.Integer(), nullable=False)
    else:
        op.drop_index("ix_subscriptions_source", table_name="subscriptions")
        op.drop_column("subscriptions", "source")
        op.drop_column("subscriptions", "revoke_reason")
        op.drop_column("subscriptions", "revoked_at")
        op.alter_column("subscriptions", "payment_id", existing_type=sa.Integer(), nullable=False)

    op.drop_column("payments", "refunded_at")
    op.drop_column("payments", "failed_at")
    op.drop_column("payments", "paid_at")
    op.drop_column("payments", "provider_payload")
    op.drop_column("plans", "is_active")
