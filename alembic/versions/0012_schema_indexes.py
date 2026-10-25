import sqlalchemy as sa

from alembic import op

revision = "0012_schema_indexes"
down_revision = "0011_access_operations"
branch_labels = None
depends_on = None

UNIQUE_INDEXES = (
    ("admins", "telegram_id"),
    ("app_settings", "key"),
    ("channels", "telegram_chat_id"),
    ("promo_codes", "code"),
    ("users", "telegram_id"),
    ("webhook_events", "event_id"),
)
ADDED_INDEXES = (
    ("payments", "user_id"),
    ("payments", "plan_id"),
    ("subscriptions", "user_id"),
    ("subscriptions", "channel_id"),
    ("webhook_events", "event_type"),
)
LEGACY_CONSTRAINTS = {
    "admins": "admins_telegram_id_key",
    "app_settings": "app_settings_key_key",
    "channels": "channels_telegram_chat_id_key",
    "promo_codes": "uq_promo_codes_code",
    "users": "users_telegram_id_key",
    "webhook_events": "webhook_events_event_id_key",
}
NAMING_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def upgrade() -> None:
    for table, column in UNIQUE_INDEXES:
        name = f"ix_{table}_{column}"
        op.drop_index(name, table_name=table)
        op.create_index(name, table, [column], unique=True)
        if op.get_context().as_sql and op.get_context().dialect.name == "postgresql":
            for constraint in dict.fromkeys((LEGACY_CONSTRAINTS[table], f"uq_{table}_{column}")):
                op.execute(f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{constraint}"')
            continue
        constraints = sa.inspect(op.get_bind()).get_unique_constraints(table)
        matching = [item for item in constraints if item["column_names"] == [column]]
        if matching:
            with op.batch_alter_table(table, naming_convention=NAMING_CONVENTION) as batch:
                for constraint in matching:
                    batch.drop_constraint(
                        constraint["name"] or f"uq_{table}_{column}", type_="unique"
                    )
    for table, column in ADDED_INDEXES:
        op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade() -> None:
    for table, column in reversed(ADDED_INDEXES):
        op.drop_index(f"ix_{table}_{column}", table_name=table)
    for table, column in reversed(UNIQUE_INDEXES):
        name = f"ix_{table}_{column}"
        with op.batch_alter_table(table) as batch:
            batch.create_unique_constraint(f"uq_{table}_{column}", [column])
        op.drop_index(name, table_name=table)
        op.create_index(name, table, [column])
