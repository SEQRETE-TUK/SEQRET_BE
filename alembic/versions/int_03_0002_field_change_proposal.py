"""persist field issues and quoted change proposal details

Revision ID: int_03_0002
Revises: int_02_0001
Create Date: 2026-08-15 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "int_03_0002"
down_revision: str | Sequence[str] | None = "int_02_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add worker field reports and immutable quote metadata for A-07 changes."""

    op.create_table(
        "field_issue",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("client_reference", sa.Uuid(), nullable=False),
        sa.Column("base_scope_version_id", sa.Uuid(), nullable=False),
        sa.Column("reported_by_participant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "issue_type",
            sa.Enum(
                "OUT_OF_SCOPE",
                "DAMAGE_RISK",
                "SITE_BLOCKER",
                name="field_issue_type",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "issue_type IN ('OUT_OF_SCOPE', 'DAMAGE_RISK', 'SITE_BLOCKER')",
            name="field_issue_type",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["move_job.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["base_scope_version_id"],
            ["scope_version.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reported_by_participant_id"],
            ["job_participant.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "client_reference"),
    )
    op.create_index(
        "ix_field_issue_job_created",
        "field_issue",
        ["job_id", "created_at", "id"],
    )
    op.create_table(
        "field_issue_evidence",
        sa.Column("field_issue_id", sa.Uuid(), nullable=False),
        sa.Column("media_asset_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["field_issue_id"],
            ["field_issue.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["media_asset_id"],
            ["media_asset.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("field_issue_id", "media_asset_id"),
    )
    op.create_table(
        "change_proposal_detail",
        sa.Column("change_request_id", sa.Uuid(), nullable=False),
        sa.Column("field_issue_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("base_amount_krw", sa.BigInteger(), nullable=False),
        sa.Column("adjustments", sa.JSON(), nullable=False),
        sa.Column("total_amount_krw", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "base_amount_krw >= 0 AND total_amount_krw >= 0",
            name="change_proposal_amount_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["change_request_id"],
            ["change_request.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["field_issue_id"],
            ["field_issue.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("change_request_id"),
        sa.UniqueConstraint("field_issue_id"),
    )


def downgrade() -> None:
    """Refuse to discard field reports or customer-facing change history."""

    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                "LOCK TABLE change_proposal_detail, field_issue_evidence, field_issue "
                "IN ACCESS EXCLUSIVE MODE"
            )
        )
    if connection.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM field_issue) "
            "OR EXISTS (SELECT 1 FROM change_proposal_detail)"
        )
    ):
        raise RuntimeError(
            "INT-03 field issue or change proposal history exists; "
            "roll back the application without downgrading the schema"
        )
    op.drop_table("change_proposal_detail")
    op.drop_table("field_issue_evidence")
    op.drop_index("ix_field_issue_job_created", table_name="field_issue")
    op.drop_table("field_issue")
