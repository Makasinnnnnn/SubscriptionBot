"""Persist materials-channel invite lifecycle.

Revision ID: 0005_materials_invite_lifecycle
Revises: 0004_plan_stars_price
"""

import sqlalchemy as sa

from alembic import op

revision = "0005_materials_invite_lifecycle"
down_revision = "0004_plan_stars_price"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("invite_links", sa.Column("materials_channel_id", sa.BigInteger(), nullable=True))
    op.add_column("invite_links", sa.Column("materials_link", sa.String(length=512), nullable=True))
    op.add_column("invite_links", sa.Column("materials_expire_at", sa.DateTime(), nullable=True))
    op.add_column(
        "invite_links",
        sa.Column("materials_is_revoked", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("invite_links", sa.Column("materials_revoked_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("invite_links", "materials_revoked_at")
    op.drop_column("invite_links", "materials_is_revoked")
    op.drop_column("invite_links", "materials_expire_at")
    op.drop_column("invite_links", "materials_link")
    op.drop_column("invite_links", "materials_channel_id")
