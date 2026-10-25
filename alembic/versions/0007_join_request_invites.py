"""Track Telegram join-request invite mode.

Revision ID: 0007_join_request_invites
Revises: 0006_payment_provider_identity
"""

import sqlalchemy as sa

from alembic import op

revision = "0007_join_request_invites"
down_revision = "0006_payment_provider_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invite_links",
        sa.Column(
            "primary_join_request_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "invite_links",
        sa.Column(
            "materials_join_request_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("invite_links", "materials_join_request_enabled")
    op.drop_column("invite_links", "primary_join_request_enabled")
