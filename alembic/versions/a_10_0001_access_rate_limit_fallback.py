"""access rate limit fallback

Revision ID: a_10_0001
Revises: a_09_0001
Create Date: 2026-08-12 16:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a_10_0001"
down_revision: str | Sequence[str] | None = "a_09_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the recoverable fixed-window state to existing access links."""

    with op.batch_alter_table("participant_access_token") as batch_op:
        batch_op.add_column(
            sa.Column("rate_window_started_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "rate_window_count",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "rate_window_state",
            "(rate_window_started_at IS NULL AND rate_window_count = 0) OR "
            "(rate_window_started_at IS NOT NULL AND rate_window_count > 0)",
        )


def downgrade() -> None:
    """Remove the database fallback counter."""

    with op.batch_alter_table("participant_access_token") as batch_op:
        batch_op.drop_constraint("rate_window_state", type_="check")
        batch_op.drop_column("rate_window_count")
        batch_op.drop_column("rate_window_started_at")
