"""store vehicle and staffing quote snapshot

Revision ID: a_23_0001
Revises: b_08_0001
Create Date: 2026-08-17 11:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a_23_0001"
down_revision: str | Sequence[str] | None = "b_08_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Allow legacy proposals to remain readable while new commands require a plan."""

    op.add_column(
        "scope_proposal",
        sa.Column("execution_plan", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    """Refuse to discard any structured execution plan snapshot."""

    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE scope_proposal IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(
        sa.text("SELECT 1 FROM scope_proposal WHERE execution_plan IS NOT NULL LIMIT 1")
    ):
        raise RuntimeError(
            "scope execution plan rows exist; roll back the application without downgrading the schema"
        )
    op.drop_column("scope_proposal", "execution_plan")
