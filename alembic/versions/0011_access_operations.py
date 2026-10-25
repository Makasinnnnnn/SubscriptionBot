import sqlalchemy as sa

from alembic import op

revision = "0011_access_operations"
down_revision = "0010_payment_integrity"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "access_operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("operation_key", sa.String(255), nullable=False, unique=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payment_id", sa.Integer(), sa.ForeignKey("payments.id", ondelete="CASCADE")),
        sa.Column(
            "subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.id", ondelete="CASCADE")
        ),
        sa.Column("chat_id", sa.BigInteger()),
        sa.Column("telegram_id", sa.BigInteger()),
        sa.Column("invite_link", sa.String(512)),
        sa.Column("expires_at_snapshot", sa.DateTime()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("last_error", sa.String(255)),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_access_operations_next_attempt_at", "access_operations", ["next_attempt_at"]
    )
    op.create_index("ix_access_operations_completed_at", "access_operations", ["completed_at"])
    for suffix, kind, chat, link, joins, predicate in [
        ("primary-member", "remove_member", "c.telegram_chat_id", "NULL", "", ""),
        (
            "materials-member",
            "remove_member",
            "i.materials_channel_id",
            "NULL",
            "JOIN invite_links i ON i.subscription_id = s.id",
            "AND i.materials_channel_id IS NOT NULL",
        ),
        (
            "primary-link",
            "revoke_link",
            "c.telegram_chat_id",
            "i.link",
            "JOIN invite_links i ON i.subscription_id = s.id",
            "AND i.link IS NOT NULL AND i.link <> ''",
        ),
        (
            "materials-link",
            "revoke_link",
            "i.materials_channel_id",
            "i.materials_link",
            "JOIN invite_links i ON i.subscription_id = s.id",
            "AND i.materials_channel_id IS NOT NULL AND i.materials_link IS NOT NULL AND i.materials_link <> ''",
        ),
    ]:
        op.execute(
            sa.text(f"""
            INSERT INTO access_operations
                (operation_key, kind, subscription_id, telegram_id, chat_id, invite_link, expires_at_snapshot, attempts, next_attempt_at)
            SELECT 'legacy:{suffix}:' || CAST(s.id AS TEXT), '{kind}', s.id, u.telegram_id,
                {chat}, {link}, s.expires_at, 0, CURRENT_TIMESTAMP
            FROM subscriptions s JOIN users u ON u.id = s.user_id JOIN channels c ON c.id = s.channel_id {joins}
            WHERE s.status IN ('expired', 'cancelled') {predicate}
        """)
        )


def downgrade():
    op.drop_table("access_operations")
