"""Isolated educational preferences and versioned signal context."""

import sqlalchemy as sa

from alembic import op

revision = "0013_education"
down_revision = "0012_schema_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "education_preferences",
        sa.Column("telegram_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("values", sa.JSON(), nullable=False),
    )
    op.create_table(
        "education_signals",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("education_signals")
    op.drop_table("education_preferences")
