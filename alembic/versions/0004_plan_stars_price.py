"""Add a provider-specific Telegram Stars price to plans.

Revision ID: 0004_plan_stars_price
Revises: 0003_legal_admin_notifications
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_plan_stars_price"
down_revision = "0003_legal_admin_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plans", sa.Column("stars_price", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("plans", "stars_price")
