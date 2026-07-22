"""Add legal acceptance and admin notification settings.

Revision ID: 0003_legal_admin_notifications
Revises: 0002_admin_access_lifecycle
Create Date: 2026-07-22
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_legal_admin_notifications"
down_revision = "0002_admin_access_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "legal_acceptances",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("legal_version", sa.String(length=64), nullable=False),
        sa.Column("terms_version", sa.String(length=64), nullable=False),
        sa.Column("privacy_version", sa.String(length=64), nullable=False),
        sa.Column("refund_policy_version", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "legal_version", name="uq_legal_user_version"),
    )
    op.create_index("ix_legal_acceptances_user_id", "legal_acceptances", ["user_id"])
    op.create_index("ix_legal_acceptances_telegram_id", "legal_acceptances", ["telegram_id"])
    op.create_index("ix_legal_acceptances_legal_version", "legal_acceptances", ["legal_version"])

    op.create_table(
        "admin_notification_preferences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("telegram_id", "category", name="uq_admin_notification_category"),
    )
    op.create_index("ix_admin_notification_preferences_telegram_id", "admin_notification_preferences", ["telegram_id"])
    op.create_index("ix_admin_notification_preferences_category", "admin_notification_preferences", ["category"])

    op.create_table(
        "admin_notification_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("deduplication_key", sa.String(length=255), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("telegram_id", "deduplication_key", name="uq_admin_notification_delivery"),
    )
    op.create_index("ix_admin_notification_deliveries_telegram_id", "admin_notification_deliveries", ["telegram_id"])
    op.create_index("ix_admin_notification_deliveries_category", "admin_notification_deliveries", ["category"])
    op.create_index("ix_admin_notification_deliveries_deduplication_key", "admin_notification_deliveries", ["deduplication_key"])


def downgrade() -> None:
    op.drop_table("admin_notification_deliveries")
    op.drop_table("admin_notification_preferences")
    op.drop_table("legal_acceptances")
