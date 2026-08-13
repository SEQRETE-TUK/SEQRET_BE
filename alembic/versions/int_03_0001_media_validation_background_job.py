"""add media validation background jobs

Revision ID: int_03_0001
Revises: a_08_0002
Create Date: 2026-08-13 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "int_03_0001"
down_revision: str | Sequence[str] | None = "a_08_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Extend the existing durable job state machine with validation snapshots."""

    with op.batch_alter_table("background_job") as batch_op:
        batch_op.add_column(sa.Column("target_content_type", sa.String(length=255)))
        batch_op.add_column(sa.Column("target_size_bytes", sa.Integer()))
        batch_op.drop_constraint("background_job_type", type_="check")
        batch_op.create_check_constraint(
            "background_job_type",
            "job_type IN ('MEDIA_VALIDATION', 'MEDIA_RETENTION_DELETE')",
        )
        batch_op.create_check_constraint(
            "background_job_target_shape",
            "(job_type = 'MEDIA_VALIDATION' AND target_content_type IS NOT NULL "
            "AND length(target_content_type) > 0 AND target_size_bytes > 0) OR "
            "(job_type = 'MEDIA_RETENTION_DELETE' AND target_content_type IS NULL "
            "AND target_size_bytes IS NULL)",
        )


def downgrade() -> None:
    """Refuse to discard active or historical validation jobs."""

    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE background_job IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(
        sa.text("SELECT 1 FROM background_job WHERE job_type = 'MEDIA_VALIDATION' LIMIT 1")
    ):
        raise RuntimeError(
            "INT-03 media validation jobs exist; "
            "roll back the application without downgrading the schema"
        )

    with op.batch_alter_table("background_job") as batch_op:
        batch_op.drop_constraint("background_job_target_shape", type_="check")
        batch_op.drop_constraint("background_job_type", type_="check")
        batch_op.create_check_constraint(
            "background_job_type",
            "job_type IN ('MEDIA_RETENTION_DELETE')",
        )
        batch_op.drop_column("target_size_bytes")
        batch_op.drop_column("target_content_type")
