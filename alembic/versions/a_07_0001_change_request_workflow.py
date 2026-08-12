"""change request workflow

Revision ID: a_07_0001
Revises: a_06_0001
Create Date: 2026-08-12 13:01:17.494388

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a_07_0001"
down_revision: str | Sequence[str] | None = "a_06_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "change_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("base_scope_version_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_participant_id", sa.Uuid(), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=False),
        sa.Column("proposed_content", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "CLARIFICATION_REQUESTED",
                "APPROVED",
                "REJECTED",
                name="change_request_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("clarification_requested_by_participant_id", sa.Uuid(), nullable=True),
        sa.Column("clarification_request", sa.String(length=2000), nullable=True),
        sa.Column("clarification_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("explanation", sa.String(length=2000), nullable=True),
        sa.Column("explained_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_participant_id", sa.Uuid(), nullable=True),
        sa.Column("decision_note", sa.String(length=2000), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_scope_version_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(clarification_requested_by_participant_id IS NULL AND clarification_request IS NULL AND clarification_requested_at IS NULL AND explanation IS NULL AND explained_at IS NULL) OR (clarification_requested_by_participant_id IS NOT NULL AND clarification_request IS NOT NULL AND clarification_requested_at IS NOT NULL AND ((status = 'CLARIFICATION_REQUESTED' AND explanation IS NULL AND explained_at IS NULL) OR (status IN ('PENDING', 'APPROVED', 'REJECTED') AND explanation IS NOT NULL AND explained_at IS NOT NULL)))",
            name="change_request_clarification",
        ),
        sa.CheckConstraint(
            "(status IN ('PENDING', 'CLARIFICATION_REQUESTED') AND decided_by_participant_id IS NULL AND decided_at IS NULL AND decision_note IS NULL AND result_scope_version_id IS NULL) OR (status = 'APPROVED' AND decided_by_participant_id IS NOT NULL AND decided_at IS NOT NULL AND result_scope_version_id IS NOT NULL) OR (status = 'REJECTED' AND decided_by_participant_id IS NOT NULL AND decided_at IS NOT NULL AND decision_note IS NOT NULL AND result_scope_version_id IS NULL)",
            name="change_request_decision",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'CLARIFICATION_REQUESTED', 'APPROVED', 'REJECTED')",
            name="change_request_status",
        ),
        sa.ForeignKeyConstraint(
            ["base_scope_version_id"],
            ["scope_version.id"],
            name=op.f("fk_change_request_base_scope_version_id_scope_version"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["clarification_requested_by_participant_id"],
            ["job_participant.id"],
            name=op.f(
                "fk_change_request_clarification_requested_by_participant_id_job_participant"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_participant_id"],
            ["job_participant.id"],
            name=op.f("fk_change_request_decided_by_participant_id_job_participant"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["move_job.id"],
            name=op.f("fk_change_request_job_id_move_job"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_participant_id"],
            ["job_participant.id"],
            name=op.f("fk_change_request_requested_by_participant_id_job_participant"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["result_scope_version_id"],
            ["scope_version.id"],
            name=op.f("fk_change_request_result_scope_version_id_scope_version"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_change_request")),
        sa.UniqueConstraint(
            "result_scope_version_id", name=op.f("uq_change_request_result_scope_version_id")
        ),
    )
    op.create_index(op.f("ix_change_request_job_id"), "change_request", ["job_id"], unique=False)
    op.create_table(
        "change_request_evidence",
        sa.Column("change_request_id", sa.Uuid(), nullable=False),
        sa.Column("media_asset_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["change_request_id"],
            ["change_request.id"],
            name=op.f("fk_change_request_evidence_change_request_id_change_request"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["media_asset_id"],
            ["media_asset.id"],
            name=op.f("fk_change_request_evidence_media_asset_id_media_asset"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "change_request_id", "media_asset_id", name=op.f("pk_change_request_evidence")
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("change_request_evidence")
    op.drop_index(op.f("ix_change_request_job_id"), table_name="change_request")
    op.drop_table("change_request")
