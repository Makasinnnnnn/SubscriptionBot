"""Independent trade-journal schema; never alters subscription tables."""

import sqlalchemy as sa

from alembic import op

revision = "j0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("journal_mutex", sa.Column("id", sa.Integer, primary_key=True))
    op.create_table("journal_accounts", sa.Column("id", sa.String(80), primary_key=True),
                    sa.Column("payload", sa.JSON, nullable=False))
    op.create_table("journal_slots", sa.Column("id", sa.String(40), primary_key=True),
                    sa.Column("blocked", sa.Boolean, nullable=False),
                    sa.Column("baseline", sa.JSON, nullable=False))
    op.create_table("journal_trades", sa.Column("id", sa.String(40), primary_key=True),
                    sa.Column("author", sa.BigInteger, nullable=False),
                    sa.Column("payload", sa.JSON, nullable=False),
                    sa.Column("version", sa.Integer, nullable=False),
                    sa.Column("synced_version", sa.Integer, nullable=False),
                    sa.Column("baseline", sa.JSON, nullable=False),
                    sa.Column("synced_at", sa.String(50), nullable=True))
    op.create_table("journal_drafts", sa.Column("user_id", sa.BigInteger, primary_key=True),
                    sa.Column("payload", sa.JSON, nullable=False))
    op.create_table("journal_events", sa.Column("id", sa.String(160), primary_key=True),
                    sa.Column("response", sa.JSON, nullable=False))
    op.create_table("journal_jobs", sa.Column("id", sa.String(160), primary_key=True),
                    sa.Column("trade_id", sa.String(40), nullable=False),
                    sa.Column("kind", sa.String(20), nullable=False),
                    sa.Column("payload", sa.JSON, nullable=False),
                    sa.Column("status", sa.String(20), nullable=False),
                    sa.Column("attempts", sa.Integer, nullable=False),
                    sa.Column("next_at", sa.Float, nullable=False),
                    sa.Column("error", sa.Text, nullable=True))
    for column in ("trade_id", "status", "next_at"):
        op.create_index(f"ix_journal_jobs_{column}", "journal_jobs", [column])
    op.create_table("journal_screenshots", sa.Column("id", sa.String(80), primary_key=True),
                    sa.Column("trade_id", sa.String(40), nullable=False),
                    sa.Column("telegram_file", sa.String(300), nullable=False),
                    sa.Column("filename", sa.String(180), nullable=False),
                    sa.Column("mime", sa.String(60), nullable=False),
                    sa.Column("content", sa.LargeBinary, nullable=True),
                    sa.Column("kind", sa.String(40), nullable=False),
                    sa.Column("captured", sa.String(50), nullable=False),
                    sa.Column("comment", sa.Text, nullable=False),
                    sa.Column("drive_id", sa.String(160), nullable=True),
                    sa.Column("drive_url", sa.Text, nullable=True),
                    sa.Column("sheet_row", sa.Integer, nullable=True))
    op.create_index("ix_journal_screenshots_trade_id", "journal_screenshots", ["trade_id"])
    op.create_table("journal_audit", sa.Column("id", sa.String(80), primary_key=True),
                    sa.Column("user_id", sa.BigInteger, nullable=False),
                    sa.Column("action", sa.String(40), nullable=False),
                    sa.Column("trade_id", sa.String(40), nullable=False),
                    sa.Column("at", sa.String(50), nullable=False),
                    sa.Column("payload", sa.JSON, nullable=False))
    op.execute(sa.text("INSERT INTO journal_mutex (id) VALUES (1)"))


def downgrade():
    for name in ("audit", "screenshots", "jobs", "events", "drafts", "trades", "slots", "accounts", "mutex"):
        op.drop_table("journal_" + name)
