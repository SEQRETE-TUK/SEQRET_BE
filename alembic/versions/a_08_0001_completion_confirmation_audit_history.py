"""completion confirmation audit history

Revision ID: a_08_0001
Revises: a_07_0001
Create Date: 2026-08-12 13:59:17.239835

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a_08_0001"
down_revision: str | Sequence[str] | None = "a_07_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "audit_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column(
            "event_type",
            sa.Enum(
                "JOB_CREATED",
                "PARTICIPANT_CONNECTED",
                "ACCESS_LINK_ISSUED",
                "ACCESS_LINK_REVOKED",
                "COMPLETION_MEDIA_UPLOADED",
                "SCOPE_VERSION_CREATED",
                "SCOPE_VERSION_APPROVED",
                "SCOPE_VERSION_LOCKED",
                "CHANGE_REQUESTED",
                "CHANGE_CLARIFICATION_REQUESTED",
                "CHANGE_EXPLAINED",
                "CHANGE_APPROVED",
                "CHANGE_REJECTED",
                "COMPLETION_CONFIRMED",
                "JOB_COMPLETED",
                name="audit_event_type",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("actor_participant_id", sa.Uuid(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('JOB_CREATED', 'PARTICIPANT_CONNECTED', 'ACCESS_LINK_ISSUED', 'ACCESS_LINK_REVOKED', 'COMPLETION_MEDIA_UPLOADED', 'SCOPE_VERSION_CREATED', 'SCOPE_VERSION_APPROVED', 'SCOPE_VERSION_LOCKED', 'CHANGE_REQUESTED', 'CHANGE_CLARIFICATION_REQUESTED', 'CHANGE_EXPLAINED', 'CHANGE_APPROVED', 'CHANGE_REJECTED', 'COMPLETION_CONFIRMED', 'JOB_COMPLETED')",
            name="audit_event_type",
        ),
        sa.ForeignKeyConstraint(
            ["actor_participant_id"],
            ["job_participant.id"],
            name=op.f("fk_audit_event_actor_participant_id_job_participant"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["move_job.id"],
            name=op.f("fk_audit_event_job_id_move_job"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_event")),
    )
    op.create_index(
        "ix_audit_event_job_occurred", "audit_event", ["job_id", "occurred_at", "id"], unique=False
    )
    op.create_table(
        "completion_confirmation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("scope_version_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "CUSTOMER",
                "COMPANY_MANAGER",
                "FIELD_WORKER",
                name="participant_role",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "confirmed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('CUSTOMER', 'COMPANY_MANAGER')", name="completion_confirmation_role"
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["move_job.id"],
            name=op.f("fk_completion_confirmation_job_id_move_job"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"],
            ["job_participant.id"],
            name=op.f("fk_completion_confirmation_participant_id_job_participant"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scope_version_id"],
            ["scope_version.id"],
            name=op.f("fk_completion_confirmation_scope_version_id_scope_version"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_completion_confirmation")),
        sa.UniqueConstraint("job_id", "role", name=op.f("uq_completion_confirmation_job_id")),
    )
    op.create_table(
        "completion_evidence",
        sa.Column("confirmation_id", sa.Uuid(), nullable=False),
        sa.Column("media_asset_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["confirmation_id"],
            ["completion_confirmation.id"],
            name=op.f("fk_completion_evidence_confirmation_id_completion_confirmation"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["media_asset_id"],
            ["media_asset.id"],
            name=op.f("fk_completion_evidence_media_asset_id_media_asset"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "confirmation_id", "media_asset_id", name=op.f("pk_completion_evidence")
        ),
    )
    op.add_column("move_job", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        sa.text(
            "UPDATE move_job SET completed_at = updated_at "
            "WHERE status = 'COMPLETED' AND completed_at IS NULL"
        )
    )
    with op.batch_alter_table("move_job") as batch_op:
        batch_op.create_check_constraint(
            "move_job_completion",
            "(status = 'COMPLETED') = (completed_at IS NOT NULL)",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("move_job") as batch_op:
        batch_op.drop_constraint("move_job_completion", type_="check")
        batch_op.drop_column("completed_at")
    op.drop_table("completion_evidence")
    op.drop_table("completion_confirmation")
    op.drop_index("ix_audit_event_job_occurred", table_name="audit_event")
    op.drop_table("audit_event")
