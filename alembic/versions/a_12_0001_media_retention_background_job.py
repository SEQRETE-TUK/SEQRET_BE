"""media retention background job

Revision ID: a_12_0001
Revises: a_10_0001
Create Date: 2026-08-12 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a_12_0001"
down_revision: str | Sequence[str] | None = "a_10_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add one durable media-deletion target."""

    op.create_table(
        "background_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("move_job_id", sa.Uuid(), nullable=False),
        sa.Column("media_asset_id", sa.Uuid(), nullable=False),
        sa.Column(
            "job_type",
            sa.Enum(
                "MEDIA_RETENTION_DELETE",
                name="background_job_type",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "DISPATCHING",
                "QUEUED",
                "RUNNING",
                "SUCCEEDED",
                "FAILED",
                name="background_job_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("target_object_key", sa.String(length=1024), nullable=False),
        sa.Column("target_generation", sa.String(length=255), nullable=False),
        sa.Column("trace_id", sa.String(length=32), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("dispatch_token", sa.Uuid(), nullable=True),
        sa.Column("dispatch_locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_task_id", sa.String(length=512), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("created_by_participant_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt_count >= 0", name="background_job_attempt_nonnegative"),
        sa.CheckConstraint(
            "(attempt_count = 0) = (last_attempt_at IS NULL)",
            name="background_job_attempt_time",
        ),
        sa.CheckConstraint(
            "(status = 'DISPATCHING' AND dispatch_token IS NOT NULL "
            "AND dispatch_locked_until IS NOT NULL) OR "
            "(status <> 'DISPATCHING' AND dispatch_token IS NULL "
            "AND dispatch_locked_until IS NULL)",
            name="background_job_dispatch_lease",
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR length(last_error_code) > 0",
            name="background_job_error_code_present",
        ),
        sa.CheckConstraint(
            "(status = 'FAILED') = (last_error_code IS NOT NULL)",
            name="background_job_failure_code",
        ),
        sa.CheckConstraint(
            "(status = 'RUNNING') = (execution_deadline_at IS NOT NULL)",
            name="background_job_execution_deadline",
        ),
        sa.CheckConstraint(
            "job_type IN ('MEDIA_RETENTION_DELETE')",
            name="background_job_type",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'DISPATCHING', 'QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')",
            name="background_job_status",
        ),
        sa.CheckConstraint(
            "length(target_object_key) > 0",
            name="background_job_target_present",
        ),
        sa.CheckConstraint(
            "length(target_generation) > 0",
            name="background_job_generation_present",
        ),
        sa.CheckConstraint("length(trace_id) = 32", name="background_job_trace_id_length"),
        sa.CheckConstraint(
            "(status IN ('SUCCEEDED', 'FAILED')) = (completed_at IS NOT NULL)",
            name="background_job_completion_time",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_participant_id"],
            ["job_participant.id"],
            name=op.f("fk_background_job_created_by_participant_id_job_participant"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["media_asset_id"],
            ["media_asset.id"],
            name=op.f("fk_background_job_media_asset_id_media_asset"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["move_job_id"],
            ["move_job.id"],
            name=op.f("fk_background_job_move_job_id_move_job"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_background_job")),
        sa.UniqueConstraint(
            "job_type",
            "media_asset_id",
            name="uq_background_job_type_media_asset_id",
        ),
    )
    op.create_index(
        "ix_background_job_dispatch",
        "background_job",
        ["status", "scheduled_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_background_job_dispatch_lease",
        "background_job",
        ["status", "dispatch_locked_until", "id"],
        unique=False,
    )
    op.create_index(
        "ix_background_job_move_job_created",
        "background_job",
        ["move_job_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove background jobs and restore the earlier audit vocabulary."""

    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE background_job IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT 1 FROM background_job LIMIT 1")):
        raise RuntimeError(
            "A-12 background jobs exist; roll back the application without downgrading the schema"
        )

    op.drop_index("ix_background_job_move_job_created", table_name="background_job")
    op.drop_index("ix_background_job_dispatch_lease", table_name="background_job")
    op.drop_index("ix_background_job_dispatch", table_name="background_job")
    op.drop_table("background_job")
