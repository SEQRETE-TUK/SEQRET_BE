"""scope approval lock

Revision ID: a_05_0001
Revises: a_04_0001
Create Date: 2026-08-12 12:13:09.980361

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a_05_0001"
down_revision: str | Sequence[str] | None = "a_04_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "scope_version",
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "scope_approval",
        sa.Column("id", sa.Uuid(), nullable=False),
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
            "approved_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('CUSTOMER', 'COMPANY_MANAGER')", name="scope_approval_role"),
        sa.ForeignKeyConstraint(
            ["participant_id"],
            ["job_participant.id"],
            name=op.f("fk_scope_approval_participant_id_job_participant"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scope_version_id"],
            ["scope_version.id"],
            name=op.f("fk_scope_approval_scope_version_id_scope_version"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scope_approval")),
        sa.UniqueConstraint(
            "scope_version_id", "role", name=op.f("uq_scope_approval_scope_version_id")
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("scope_approval")
    op.drop_column("scope_version", "locked_at")
