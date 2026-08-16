"""store structured origin and destination work conditions

Revision ID: a_16_0001
Revises: int_04_0001
Create Date: 2026-08-17 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a_16_0001"
down_revision: str | Sequence[str] | None = "int_04_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UNKNOWN_CONDITIONS = (
    '{"residence_type":"unknown",'
    '"floor":{"status":"unknown","value":null},'
    '"elevator":"unknown","stairs":"unknown",'
    '"parking_access":"unknown",'
    '"carry_distance":{"status":"unknown","value_m":null},'
    '"access_note":null}'
)


def upgrade() -> None:
    """Backfill existing endpoints as explicitly unknown before enforcing non-null."""

    op.add_column(
        "location",
        sa.Column(
            "conditions",
            sa.JSON(),
            nullable=False,
            server_default=_UNKNOWN_CONDITIONS,
        ),
    )


def downgrade() -> None:
    """Remove structured endpoint conditions."""

    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("location") as batch:
            batch.drop_column("conditions")
    else:
        op.drop_column("location", "conditions")
