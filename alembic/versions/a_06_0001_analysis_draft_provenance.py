"""analysis draft provenance

Revision ID: a_06_0001
Revises: a_05_0001
Create Date: 2026-08-12 12:24:05.799906

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a_06_0001"
down_revision: str | Sequence[str] | None = "a_05_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("scope_version") as batch_op:
        batch_op.add_column(sa.Column("source_analysis_run_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("source_capture_session_id", sa.Uuid(), nullable=True))
        batch_op.add_column(
            sa.Column("analysis_source", sa.JSON(none_as_null=True), nullable=True)
        )
        batch_op.alter_column(
            "created_by_participant_id",
            existing_type=sa.Uuid(),
            nullable=True,
        )
        batch_op.create_unique_constraint(
            op.f("uq_scope_version_source_analysis_run_id"),
            ["source_analysis_run_id"],
        )
        batch_op.create_foreign_key(
            op.f("fk_scope_version_source_capture_session_id_capture_session"),
            "capture_session",
            ["source_capture_session_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "scope_version_origin",
            "(source_analysis_run_id IS NULL AND source_capture_session_id IS NULL "
            "AND analysis_source IS NULL AND created_by_participant_id IS NOT NULL) OR "
            "(source_analysis_run_id IS NOT NULL AND source_capture_session_id IS NOT NULL "
            "AND analysis_source IS NOT NULL AND created_by_participant_id IS NULL)",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("scope_version") as batch_op:
        batch_op.drop_constraint("scope_version_origin", type_="check")
    op.execute(
        sa.text(
            "UPDATE scope_version "
            "SET created_by_participant_id = ("
            "SELECT created_by_participant_id FROM capture_session "
            "WHERE capture_session.id = scope_version.source_capture_session_id"
            ") WHERE created_by_participant_id IS NULL"
        )
    )
    with op.batch_alter_table("scope_version") as batch_op:
        batch_op.drop_constraint(
            op.f("fk_scope_version_source_capture_session_id_capture_session"),
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            op.f("uq_scope_version_source_analysis_run_id"),
            type_="unique",
        )
        batch_op.alter_column(
            "created_by_participant_id",
            existing_type=sa.Uuid(),
            nullable=False,
        )
        batch_op.drop_column("analysis_source")
        batch_op.drop_column("source_capture_session_id")
        batch_op.drop_column("source_analysis_run_id")
