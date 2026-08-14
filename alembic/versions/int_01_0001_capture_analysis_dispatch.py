"""add capture analysis dispatch workflow

Revision ID: int_01_0001
Revises: int_03_0001
Create Date: 2026-08-15 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "int_01_0001"
down_revision: str | Sequence[str] | None = "int_03_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist capture submission and the A-owned analysis state machine."""

    op.create_table(
        "capture_analysis_dispatch",
        sa.Column("analysis_run_id", sa.Uuid(), nullable=False),
        sa.Column("capture_session_id", sa.Uuid(), nullable=False),
        sa.Column("move_job_id", sa.Uuid(), nullable=False),
        sa.Column("submitted_by_participant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "DISPATCHING",
                "QUEUED",
                "RUNNING",
                "COMPLETED",
                "FAILED",
                name="capture_analysis_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("trace_id", sa.String(length=32), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "dispatch_attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("dispatch_token", sa.Uuid()),
        sa.Column("dispatch_locked_until", sa.DateTime(timezone=True)),
        sa.Column("provider_task_id", sa.String(length=512)),
        sa.Column("last_dispatch_error_code", sa.String(length=64)),
        sa.Column("failure_code", sa.String(length=64)),
        sa.Column("retryable", sa.Boolean()),
        sa.Column("scope_version_id", sa.Uuid()),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('PENDING', 'DISPATCHING', 'QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')",
            name="capture_analysis_dispatch_status",
        ),
        sa.CheckConstraint(
            "dispatch_attempt_count >= 0",
            name="capture_analysis_dispatch_attempt_nonnegative",
        ),
        sa.CheckConstraint(
            "length(trace_id) = 32",
            name="capture_analysis_dispatch_trace_id_length",
        ),
        sa.CheckConstraint(
            "(dispatch_attempt_count = 0) = (last_attempt_at IS NULL)",
            name="capture_analysis_dispatch_attempt_time",
        ),
        sa.CheckConstraint(
            "(status = 'DISPATCHING' AND dispatch_token IS NOT NULL "
            "AND dispatch_locked_until IS NOT NULL) OR "
            "(status <> 'DISPATCHING' AND dispatch_token IS NULL "
            "AND dispatch_locked_until IS NULL)",
            name="capture_analysis_dispatch_lease",
        ),
        sa.CheckConstraint(
            "last_dispatch_error_code IS NULL OR length(last_dispatch_error_code) > 0",
            name="capture_analysis_dispatch_error_present",
        ),
        sa.CheckConstraint(
            "(status IN ('COMPLETED', 'FAILED')) = (completed_at IS NOT NULL)",
            name="capture_analysis_dispatch_completion_time",
        ),
        sa.CheckConstraint(
            "(status = 'COMPLETED') = (scope_version_id IS NOT NULL)",
            name="capture_analysis_dispatch_scope_version",
        ),
        sa.CheckConstraint(
            "(status = 'FAILED' AND failure_code IS NOT NULL AND retryable IS NOT NULL) OR "
            "(status <> 'FAILED' AND failure_code IS NULL AND retryable IS NULL)",
            name="capture_analysis_dispatch_failure",
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR length(failure_code) > 0",
            name="capture_analysis_dispatch_failure_present",
        ),
        sa.ForeignKeyConstraint(
            ["capture_session_id"],
            ["capture_session.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["move_job_id"], ["move_job.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["submitted_by_participant_id"],
            ["job_participant.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scope_version_id"],
            ["scope_version.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("analysis_run_id"),
        sa.UniqueConstraint("capture_session_id"),
        sa.UniqueConstraint("scope_version_id"),
    )
    op.create_index(
        "ix_capture_analysis_dispatch_due",
        "capture_analysis_dispatch",
        ["status", "scheduled_at", "analysis_run_id"],
    )
    op.create_index(
        "ix_capture_analysis_dispatch_lease",
        "capture_analysis_dispatch",
        ["status", "dispatch_locked_until", "analysis_run_id"],
    )


def downgrade() -> None:
    """Refuse to discard submitted capture-analysis history."""

    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE capture_analysis_dispatch IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT 1 FROM capture_analysis_dispatch LIMIT 1")):
        raise RuntimeError(
            "INT-01 capture analysis rows exist; "
            "roll back the application without downgrading the schema"
        )
    op.drop_index(
        "ix_capture_analysis_dispatch_lease",
        table_name="capture_analysis_dispatch",
    )
    op.drop_index(
        "ix_capture_analysis_dispatch_due",
        table_name="capture_analysis_dispatch",
    )
    op.drop_table("capture_analysis_dispatch")
