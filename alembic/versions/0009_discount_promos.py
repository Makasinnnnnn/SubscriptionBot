"""Turn promo codes into payment discounts.

Revision ID: 0009_discount_promos
Revises: 0008_promos_and_reminders
"""

import sqlalchemy as sa

from alembic import op

revision = "0009_discount_promos"
down_revision = "0008_promos_and_reminders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("promo_codes") as batch:
        batch.drop_constraint(
            "ck_promo_codes_redemptions_within_limit",
            type_="check",
        )
        batch.add_column(
            sa.Column(
                "discount_percent",
                sa.Integer(),
                server_default="10",
                nullable=False,
            ),
        )
        batch.create_check_constraint(
            "ck_promo_codes_discount_percent",
            "discount_percent BETWEEN 1 AND 99",
        )

    with op.batch_alter_table("payments") as batch:
        batch.add_column(
            sa.Column("promo_code_id", sa.Integer(), nullable=True),
        )
        batch.create_foreign_key(
            "fk_payments_promo_code_id",
            "promo_codes",
            ["promo_code_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index(
            "ix_payments_promo_code_id",
            ["promo_code_id"],
        )

    with op.batch_alter_table("promo_redemptions") as batch:
        batch.drop_constraint(
            "uq_promo_redemptions_code_user",
            type_="unique",
        )
        batch.add_column(
            sa.Column("payment_id", sa.Integer(), nullable=True),
        )
        batch.create_foreign_key(
            "fk_promo_redemptions_payment_id",
            "payments",
            ["payment_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_unique_constraint(
            "uq_promo_redemptions_payment_id",
            ["payment_id"],
        )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM promo_redemptions
        WHERE EXISTS (
            SELECT 1 FROM promo_redemptions AS retained
            WHERE promo_redemptions.promo_code_id = retained.promo_code_id
              AND promo_redemptions.user_id = retained.user_id
              AND promo_redemptions.id > retained.id
        )
        """
    )
    with op.batch_alter_table("promo_redemptions") as batch:
        batch.drop_constraint("uq_promo_redemptions_payment_id", type_="unique")
        batch.drop_constraint("fk_promo_redemptions_payment_id", type_="foreignkey")
        batch.drop_column("payment_id")
        batch.create_unique_constraint(
            "uq_promo_redemptions_code_user", ["promo_code_id", "user_id"]
        )

    with op.batch_alter_table("payments") as batch:
        batch.drop_index("ix_payments_promo_code_id")
        batch.drop_constraint("fk_payments_promo_code_id", type_="foreignkey")
        batch.drop_column("promo_code_id")

    op.execute(
        """
        UPDATE promo_codes
        SET max_redemptions = redemptions_count
        WHERE redemptions_count > max_redemptions
        """
    )
    with op.batch_alter_table("promo_codes") as batch:
        batch.drop_constraint("ck_promo_codes_discount_percent", type_="check")
        batch.drop_column("discount_percent")
        batch.create_check_constraint(
            "ck_promo_codes_redemptions_within_limit", "redemptions_count <= max_redemptions"
        )
