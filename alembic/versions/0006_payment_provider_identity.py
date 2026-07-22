"""Scope external payment IDs by provider.

Revision ID: 0006_payment_provider_identity
Revises: 0005_materials_invite_lifecycle
"""

from alembic import op

revision = "0006_payment_provider_identity"
down_revision = "0005_materials_invite_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    dialect_name = op.get_context().dialect.name
    if dialect_name == "sqlite":
        with op.batch_alter_table(
            "payments",
            naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"},
        ) as batch_op:
            batch_op.drop_constraint("uq_payments_provider_payment_id", type_="unique")
            batch_op.create_unique_constraint(
                "uq_payments_provider_external_id",
                ["provider", "provider_payment_id"],
            )
        return
    op.drop_constraint("payments_provider_payment_id_key", "payments", type_="unique")
    op.create_unique_constraint(
        "uq_payments_provider_external_id",
        "payments",
        ["provider", "provider_payment_id"],
    )


def downgrade() -> None:
    dialect_name = op.get_context().dialect.name
    if dialect_name == "sqlite":
        with op.batch_alter_table(
            "payments",
            naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"},
        ) as batch_op:
            batch_op.drop_constraint("uq_payments_provider_external_id", type_="unique")
            batch_op.create_unique_constraint(
                "uq_payments_provider_payment_id",
                ["provider_payment_id"],
            )
        return
    op.drop_constraint("uq_payments_provider_external_id", "payments", type_="unique")
    op.create_unique_constraint(
        "payments_provider_payment_id_key",
        "payments",
        ["provider_payment_id"],
    )
