"""Turn promo codes into payment discounts.

Revision ID: 0009_discount_promos
Revises: 0008_promos_and_reminders
"""

from alembic import op
import sqlalchemy as sa

revision = "0009_discount_promos"
down_revision = "0008_promos_and_reminders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_promo_codes_redemptions_within_limit",
        "promo_codes",
        type_="check",
    )
    op.add_column(
        "promo_codes",
        sa.Column(
            "discount_percent",
            sa.Integer(),
            server_default="10",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_promo_codes_discount_percent",
        "promo_codes",
        "discount_percent BETWEEN 1 AND 99",
    )

    op.add_column(
        "payments",
        sa.Column("promo_code_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_payments_promo_code_id",
        "payments",
        "promo_codes",
        ["promo_code_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_payments_promo_code_id",
        "payments",
        ["promo_code_id"],
    )

    op.drop_constraint(
        "uq_promo_redemptions_code_user",
        "promo_redemptions",
        type_="unique",
    )
    op.add_column(
        "promo_redemptions",
        sa.Column("payment_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_promo_redemptions_payment_id",
        "promo_redemptions",
        "payments",
        ["payment_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_promo_redemptions_payment_id",
        "promo_redemptions",
        ["payment_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_promo_redemptions_payment_id",
        "promo_redemptions",
        type_="unique",
    )
    op.drop_constraint(
        "fk_promo_redemptions_payment_id",
        "promo_redemptions",
        type_="foreignkey",
    )
    op.drop_column("promo_redemptions", "payment_id")
    op.execute(
        """
        DELETE FROM promo_redemptions AS duplicate
        USING promo_redemptions AS retained
        WHERE duplicate.promo_code_id = retained.promo_code_id
          AND duplicate.user_id = retained.user_id
          AND duplicate.id > retained.id
        """
    )
    op.create_unique_constraint(
        "uq_promo_redemptions_code_user",
        "promo_redemptions",
        ["promo_code_id", "user_id"],
    )

    op.drop_index("ix_payments_promo_code_id", table_name="payments")
    op.drop_constraint(
        "fk_payments_promo_code_id",
        "payments",
        type_="foreignkey",
    )
    op.drop_column("payments", "promo_code_id")

    op.drop_constraint(
        "ck_promo_codes_discount_percent",
        "promo_codes",
        type_="check",
    )
    op.drop_column("promo_codes", "discount_percent")
    op.execute(
        """
        UPDATE promo_codes
        SET max_redemptions = redemptions_count
        WHERE redemptions_count > max_redemptions
        """
    )
    op.create_check_constraint(
        "ck_promo_codes_redemptions_within_limit",
        "promo_codes",
        "redemptions_count <= max_redemptions",
    )
