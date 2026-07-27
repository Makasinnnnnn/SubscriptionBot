"""Add promo codes and period-aware reminder delivery.

Revision ID: 0008_promos_and_reminders
Revises: 0007_join_request_invites
"""

from alembic import op
import sqlalchemy as sa

revision = "0008_promos_and_reminders"
down_revision = "0007_join_request_invites"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("period_started_at", sa.DateTime(), nullable=True),
    )
    op.execute("UPDATE subscriptions SET period_started_at = starts_at")
    op.alter_column(
        "subscriptions",
        "period_started_at",
        existing_type=sa.DateTime(),
        nullable=False,
    )

    op.create_table(
        "promo_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column(
            "plan_id",
            sa.Integer(),
            sa.ForeignKey("plans.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("valid_from", sa.DateTime(), nullable=False),
        sa.Column("valid_until", sa.DateTime(), nullable=False),
        sa.Column("max_redemptions", sa.Integer(), nullable=False),
        sa.Column("redemptions_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("per_user_limit", sa.Integer(), server_default="1", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_by_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "max_redemptions > 0",
            name="ck_promo_codes_max_redemptions",
        ),
        sa.CheckConstraint(
            "redemptions_count >= 0",
            name="ck_promo_codes_redemptions_count",
        ),
        sa.CheckConstraint(
            "redemptions_count <= max_redemptions",
            name="ck_promo_codes_redemptions_within_limit",
        ),
        sa.CheckConstraint(
            "per_user_limit > 0",
            name="ck_promo_codes_per_user_limit",
        ),
        sa.CheckConstraint(
            "valid_until > valid_from",
            name="ck_promo_codes_valid_window",
        ),
        sa.UniqueConstraint("code", name="uq_promo_codes_code"),
    )
    op.create_index("ix_promo_codes_code", "promo_codes", ["code"])
    op.create_index("ix_promo_codes_plan_id", "promo_codes", ["plan_id"])
    op.create_index("ix_promo_codes_valid_from", "promo_codes", ["valid_from"])
    op.create_index("ix_promo_codes_valid_until", "promo_codes", ["valid_until"])
    op.create_index("ix_promo_codes_is_active", "promo_codes", ["is_active"])
    op.create_index(
        "ix_promo_codes_created_by_telegram_id",
        "promo_codes",
        ["created_by_telegram_id"],
    )

    op.create_table(
        "promo_redemptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "promo_code_id",
            sa.Integer(),
            sa.ForeignKey("promo_codes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("subscriptions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("redeemed_at", sa.DateTime(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "promo_code_id",
            "user_id",
            name="uq_promo_redemptions_code_user",
        ),
    )
    op.create_index(
        "ix_promo_redemptions_promo_code_id",
        "promo_redemptions",
        ["promo_code_id"],
    )
    op.create_index(
        "ix_promo_redemptions_user_id",
        "promo_redemptions",
        ["user_id"],
    )
    op.create_index(
        "ix_promo_redemptions_subscription_id",
        "promo_redemptions",
        ["subscription_id"],
    )
    op.create_index(
        "ix_promo_redemptions_redeemed_at",
        "promo_redemptions",
        ["redeemed_at"],
    )

    op.add_column(
        "notification_logs",
        sa.Column("expires_at_snapshot", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "notification_logs",
        sa.Column("threshold_minutes", sa.Integer(), nullable=True),
    )
    op.execute(
        """
        UPDATE notification_logs AS notification
        SET expires_at_snapshot = subscription.expires_at,
            threshold_minutes = CASE
                WHEN notification.type LIKE 'expires_%d'
                THEN CAST(
                    REPLACE(REPLACE(notification.type, 'expires_', ''), 'd', '')
                    AS INTEGER
                ) * 1440
                ELSE 1
            END
        FROM subscriptions AS subscription
        WHERE subscription.id = notification.subscription_id
        """
    )
    op.alter_column(
        "notification_logs",
        "expires_at_snapshot",
        existing_type=sa.DateTime(),
        nullable=False,
    )
    op.alter_column(
        "notification_logs",
        "threshold_minutes",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.drop_constraint(
        "uq_notification_subscription_type",
        "notification_logs",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_notification_subscription_period_threshold",
        "notification_logs",
        ["subscription_id", "expires_at_snapshot", "threshold_minutes"],
    )
    op.create_index(
        "ix_notification_logs_expires_at_snapshot",
        "notification_logs",
        ["expires_at_snapshot"],
    )
    op.create_index(
        "ix_notification_logs_threshold_minutes",
        "notification_logs",
        ["threshold_minutes"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_logs_threshold_minutes",
        table_name="notification_logs",
    )
    op.drop_index(
        "ix_notification_logs_expires_at_snapshot",
        table_name="notification_logs",
    )
    op.drop_constraint(
        "uq_notification_subscription_period_threshold",
        "notification_logs",
        type_="unique",
    )
    op.execute(
        """
        DELETE FROM notification_logs AS duplicate
        USING notification_logs AS retained
        WHERE duplicate.subscription_id = retained.subscription_id
          AND duplicate.type = retained.type
          AND duplicate.id > retained.id
        """
    )
    op.create_unique_constraint(
        "uq_notification_subscription_type",
        "notification_logs",
        ["subscription_id", "type"],
    )
    op.drop_column("notification_logs", "threshold_minutes")
    op.drop_column("notification_logs", "expires_at_snapshot")

    op.drop_table("promo_redemptions")
    op.drop_table("promo_codes")
    op.drop_column("subscriptions", "period_started_at")
