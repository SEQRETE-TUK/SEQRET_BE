"""persist quoted scope review workflow

Revision ID: int_02_0001
Revises: a_02_0002
Create Date: 2026-08-15 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "int_02_0001"
down_revision: str | Sequence[str] | None = "a_02_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add company quote snapshots and customer revision requests."""

    op.create_table(
        "scope_proposal",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("source_scope_version_id", sa.Uuid(), nullable=False),
        sa.Column("result_scope_version_id", sa.Uuid(), nullable=False),
        sa.Column("proposed_by_participant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "INITIAL",
                "REVISION",
                name="scope_proposal_kind",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "CUSTOMER_REVIEW",
                "REVISION_REQUESTED",
                "CONFIRMED",
                "SUPERSEDED",
                name="scope_proposal_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("base_amount_krw", sa.BigInteger(), nullable=False),
        sa.Column("adjustments", sa.JSON(), nullable=False),
        sa.Column("total_amount_krw", sa.BigInteger(), nullable=False),
        sa.Column("included_works", sa.JSON(), nullable=False),
        sa.Column("exclusions", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(length=2000), nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "kind IN ('INITIAL', 'REVISION')",
            name="scope_proposal_kind",
        ),
        sa.CheckConstraint(
            "status IN ('CUSTOMER_REVIEW', 'REVISION_REQUESTED', 'CONFIRMED', 'SUPERSEDED')",
            name="scope_proposal_status",
        ),
        sa.CheckConstraint(
            "base_amount_krw >= 0 AND total_amount_krw >= 0",
            name="scope_proposal_amount_nonnegative",
        ),
        sa.CheckConstraint(
            "source_scope_version_id <> result_scope_version_id",
            name="scope_proposal_distinct_versions",
        ),
        sa.CheckConstraint(
            "(status = 'CONFIRMED' AND confirmed_at IS NOT NULL) OR "
            "(status <> 'CONFIRMED' AND confirmed_at IS NULL)",
            name="scope_proposal_confirmation_time",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["move_job.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_scope_version_id"],
            ["scope_version.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["result_scope_version_id"],
            ["scope_version.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["proposed_by_participant_id"],
            ["job_participant.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_scope_version_id"),
        sa.UniqueConstraint("result_scope_version_id"),
    )
    op.create_index(
        "ix_scope_proposal_job_status_sent",
        "scope_proposal",
        ["job_id", "status", "sent_at", "id"],
    )
    op.create_table(
        "scope_revision_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("scope_proposal_id", sa.Uuid(), nullable=False),
        sa.Column("scope_version_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_participant_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(length=2000), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_by_scope_proposal_id", sa.Uuid()),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "(resolved_by_scope_proposal_id IS NULL AND resolved_at IS NULL) OR "
            "(resolved_by_scope_proposal_id IS NOT NULL AND resolved_at IS NOT NULL)",
            name="scope_revision_request_resolution",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["move_job.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["scope_proposal_id"],
            ["scope_proposal.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scope_version_id"],
            ["scope_version.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_participant_id"],
            ["job_participant.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_scope_proposal_id"],
            ["scope_proposal.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope_proposal_id"),
        sa.UniqueConstraint("scope_version_id"),
        sa.UniqueConstraint("resolved_by_scope_proposal_id"),
    )
    op.create_index(
        "ix_scope_revision_request_job_requested",
        "scope_revision_request",
        ["job_id", "requested_at", "id"],
    )


def downgrade() -> None:
    """Refuse to discard quoted scope and revision history."""

    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text("LOCK TABLE scope_revision_request, scope_proposal IN ACCESS EXCLUSIVE MODE")
        )
    if connection.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM scope_proposal) "
            "OR EXISTS (SELECT 1 FROM scope_revision_request)"
        )
    ):
        raise RuntimeError(
            "INT-02 scope proposal or revision history exists; "
            "roll back the application without downgrading the schema"
        )
    op.drop_index(
        "ix_scope_revision_request_job_requested",
        table_name="scope_revision_request",
    )
    op.drop_table("scope_revision_request")
    op.drop_index(
        "ix_scope_proposal_job_status_sent",
        table_name="scope_proposal",
    )
    op.drop_table("scope_proposal")
