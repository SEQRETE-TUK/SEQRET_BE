"""ai analysis run and detection

Revision ID: b_03_0001
Revises: a_12_0001
Create Date: 2026-08-13 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b_03_0001"
down_revision: str | Sequence[str] | None = "a_12_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add derived analysis runs and their editable detection drafts."""

    op.create_table(
        "ai_analysis_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("capture_session_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "RUNNING",
                "COMPLETED",
                "FAILED",
                name="ai_analysis_run_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("trace_id", sa.String(length=32), nullable=False),
        sa.Column("model_name", sa.String(length=100), nullable=True),
        sa.Column("model_version", sa.String(length=100), nullable=True),
        sa.Column("prompt_version", sa.String(length=100), nullable=True),
        sa.Column("result_schema_version", sa.Integer(), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt_count >= 0", name="ai_analysis_run_attempt_nonnegative"),
        sa.CheckConstraint("length(trace_id) = 32", name="ai_analysis_run_trace_id_length"),
        sa.CheckConstraint(
            "(status = 'PENDING') = (started_at IS NULL)",
            name="ai_analysis_run_started_time",
        ),
        sa.CheckConstraint(
            "(status IN ('COMPLETED', 'FAILED')) = (completed_at IS NOT NULL)",
            name="ai_analysis_run_completion_time",
        ),
        sa.CheckConstraint(
            "(status = 'FAILED') = (failure_code IS NOT NULL)",
            name="ai_analysis_run_failure_code",
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR length(failure_code) > 0",
            name="ai_analysis_run_failure_code_present",
        ),
        sa.CheckConstraint(
            "(status = 'COMPLETED') = (model_name IS NOT NULL)",
            name="ai_analysis_run_completed_model",
        ),
        sa.CheckConstraint(
            "((model_name IS NULL) = (model_version IS NULL)) "
            "AND ((model_name IS NULL) = (prompt_version IS NULL)) "
            "AND ((model_name IS NULL) = (result_schema_version IS NULL))",
            name="ai_analysis_run_result_versions",
        ),
        sa.CheckConstraint(
            "result_schema_version IS NULL OR result_schema_version = 1",
            name="ai_analysis_run_result_schema_version",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')",
            name="ai_analysis_run_status",
        ),
        sa.ForeignKeyConstraint(
            ["capture_session_id"],
            ["capture_session.id"],
            name=op.f("fk_ai_analysis_run_capture_session_id_capture_session"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_analysis_run")),
    )
    op.create_index(
        "ix_ai_analysis_run_capture_session",
        "ai_analysis_run",
        ["capture_session_id", "created_at", "id"],
        unique=False,
    )

    op.create_table(
        "detection",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("item_key", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("review_required", sa.Boolean(), nullable=False),
        sa.Column("source_media_asset_ids", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="detection_confidence_range",
        ),
        sa.CheckConstraint("length(item_key) > 0", name="detection_item_key_present"),
        sa.CheckConstraint("length(description) > 0", name="detection_description_present"),
        sa.CheckConstraint("ordinal >= 0", name="detection_ordinal_nonnegative"),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["ai_analysis_run.id"],
            name=op.f("fk_detection_analysis_run_id_ai_analysis_run"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_detection")),
    )
    op.create_index(
        "ix_detection_analysis_run",
        "detection",
        ["analysis_run_id", "ordinal", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove detections and analysis runs when no derived rows remain."""

    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text("LOCK TABLE detection, ai_analysis_run IN ACCESS EXCLUSIVE MODE")
        )
    # As the head migration, b_03 carries the operational-data guard: once any
    # background job or analysis run exists, keep the extended schema and roll
    # back the application instead of downgrading (see docs/CONTRACTS.md).
    operational_rows_exist = connection.scalar(
        sa.text("SELECT 1 FROM background_job LIMIT 1")
    ) or connection.scalar(sa.text("SELECT 1 FROM ai_analysis_run LIMIT 1"))
    if operational_rows_exist:
        raise RuntimeError(
            "operational rows exist; roll back the application without downgrading the schema"
        )

    op.drop_index("ix_detection_analysis_run", table_name="detection")
    op.drop_table("detection")
    op.drop_index("ix_ai_analysis_run_capture_session", table_name="ai_analysis_run")
    op.drop_table("ai_analysis_run")
