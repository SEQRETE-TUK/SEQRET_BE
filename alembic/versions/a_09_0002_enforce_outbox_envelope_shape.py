"""enforce outbox envelope shape

Revision ID: a_09_0002
Revises: a_03_0002
Create Date: 2026-08-13 09:34:29.602501

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a_09_0002"
down_revision: str | Sequence[str] | None = "a_03_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Reject outbox rows that the current relay cannot deserialize."""

    payload_type = "json_typeof" if op.get_bind().dialect.name == "postgresql" else "json_type"
    with op.batch_alter_table("outbox_event") as batch_op:
        batch_op.drop_constraint("outbox_schema_version_positive", type_="check")
        batch_op.create_check_constraint("outbox_schema_version_one", "schema_version = 1")
        batch_op.create_check_constraint(
            "outbox_payload_object",
            f"{payload_type}(payload) = 'object'",
        )


def downgrade() -> None:
    """Restore the broader legacy outbox envelope."""

    with op.batch_alter_table("outbox_event") as batch_op:
        batch_op.drop_constraint("outbox_payload_object", type_="check")
        batch_op.drop_constraint("outbox_schema_version_one", type_="check")
        batch_op.create_check_constraint(
            "outbox_schema_version_positive",
            "schema_version > 0",
        )
