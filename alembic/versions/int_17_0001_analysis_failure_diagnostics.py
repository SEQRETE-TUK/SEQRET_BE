"""persist safe analysis failure diagnostics

Revision ID: int_17_0001
Revises: int_12_0001
Create Date: 2026-08-21 15:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "int_17_0001"
down_revision: str | Sequence[str] | None = "int_12_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add provider-safe diagnostics to B runs and A workflow views."""

    with op.batch_alter_table("ai_analysis_run") as batch:
        batch.add_column(sa.Column("failure_stage", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("provider_status", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("failure_detail_code", sa.String(length=64), nullable=True))
        batch.create_check_constraint(
            "ai_analysis_run_failure_stage_present",
            "failure_stage IS NULL OR length(failure_stage) > 0",
        )
        batch.create_check_constraint(
            "ai_analysis_run_failure_detail_present",
            "failure_detail_code IS NULL OR length(failure_detail_code) > 0",
        )

    with op.batch_alter_table("capture_analysis_dispatch") as batch:
        batch.add_column(sa.Column("failure_stage", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("provider_status", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("failure_detail_code", sa.String(length=64), nullable=True))
        batch.create_check_constraint(
            "capture_analysis_dispatch_failure_stage_present",
            "failure_stage IS NULL OR length(failure_stage) > 0",
        )
        batch.create_check_constraint(
            "capture_analysis_dispatch_failure_detail_present",
            "failure_detail_code IS NULL OR length(failure_detail_code) > 0",
        )


def downgrade() -> None:
    """Remove optional analysis failure diagnostics."""

    with op.batch_alter_table("capture_analysis_dispatch") as batch:
        batch.drop_constraint(
            "capture_analysis_dispatch_failure_detail_present",
            type_="check",
        )
        batch.drop_constraint(
            "capture_analysis_dispatch_failure_stage_present",
            type_="check",
        )
        batch.drop_column("failure_detail_code")
        batch.drop_column("provider_status")
        batch.drop_column("failure_stage")

    with op.batch_alter_table("ai_analysis_run") as batch:
        batch.drop_constraint("ai_analysis_run_failure_detail_present", type_="check")
        batch.drop_constraint("ai_analysis_run_failure_stage_present", type_="check")
        batch.drop_column("failure_detail_code")
        batch.drop_column("provider_status")
        batch.drop_column("failure_stage")
