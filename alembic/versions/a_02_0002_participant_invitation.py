"""add role invitation lifecycle

Revision ID: a_02_0002
Revises: int_01_0001
Create Date: 2026-08-15 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a_02_0002"
down_revision: str | Sequence[str] | None = "int_01_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist one revocable invitation for each non-customer job role."""

    op.create_table(
        "participant_invitation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("issuer_participant_id", sa.Uuid(), nullable=False),
        sa.Column("invitee_participant_id", sa.Uuid(), nullable=False),
        sa.Column("access_link_id", sa.Uuid(), nullable=False),
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
            "status",
            sa.Enum(
                "PENDING",
                "ACCEPTED",
                "DECLINED",
                "EXPIRED",
                "REVOKED",
                name="participant_invitation_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('COMPANY_MANAGER', 'FIELD_WORKER')",
            name="participant_invitation_role",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'ACCEPTED', 'DECLINED', 'EXPIRED', 'REVOKED')",
            name="participant_invitation_status",
        ),
        sa.CheckConstraint(
            "(status IN ('PENDING', 'EXPIRED') AND resolved_at IS NULL) OR "
            "(status IN ('ACCEPTED', 'DECLINED', 'REVOKED') "
            "AND resolved_at IS NOT NULL)",
            name="participant_invitation_resolution",
        ),
        sa.CheckConstraint(
            "expires_at > issued_at",
            name="participant_invitation_expiry_after_issue",
        ),
        sa.CheckConstraint(
            "issuer_participant_id <> invitee_participant_id",
            name="participant_invitation_distinct_participants",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["move_job.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["issuer_participant_id"],
            ["job_participant.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["invitee_participant_id"],
            ["job_participant.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["access_link_id"],
            ["participant_access_token.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("access_link_id"),
        sa.UniqueConstraint("job_id", "role"),
    )
    op.create_index(
        "ix_participant_invitation_job_status",
        "participant_invitation",
        ["job_id", "status", "id"],
    )


def downgrade() -> None:
    """Refuse to discard invitation history once any invitation exists."""

    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE participant_invitation IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT 1 FROM participant_invitation LIMIT 1")):
        raise RuntimeError(
            "A-02 participant invitation rows exist; "
            "roll back the application without downgrading the schema"
        )
    op.drop_index(
        "ix_participant_invitation_job_status",
        table_name="participant_invitation",
    )
    op.drop_table("participant_invitation")
