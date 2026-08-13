"""guard operational event history from schema downgrade

Revision ID: a_09_0003
Revises: b_03_0001
Create Date: 2026-08-13 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a_09_0003"
down_revision: str | Sequence[str] | None = "b_03_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Keep the schema unchanged while making rollback policy explicit."""


def downgrade() -> None:
    """Refuse to discard operational event history."""

    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                "LOCK TABLE event_consumption, notification_delivery, outbox_event "
                "IN ACCESS EXCLUSIVE MODE"
            )
        )
    if connection.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM event_consumption) "
            "OR EXISTS (SELECT 1 FROM notification_delivery) "
            "OR EXISTS (SELECT 1 FROM outbox_event)"
        )
    ):
        raise RuntimeError(
            "operational event rows exist; roll back the application without downgrading the schema"
        )
