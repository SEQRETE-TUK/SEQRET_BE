"""immutable scope version

Revision ID: a_04_0001
Revises: a_03_0001
Create Date: 2026-08-12 11:58:39.301673

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a_04_0001"
down_revision: str | Sequence[str] | None = "a_03_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "scope_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("parent_version_id", sa.Uuid(), nullable=True),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_participant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(content_hash) = 64", name="content_hash_length"),
        sa.CheckConstraint("sequence_number > 0", name="sequence_positive"),
        sa.ForeignKeyConstraint(
            ["created_by_participant_id"],
            ["job_participant.id"],
            name=op.f("fk_scope_version_created_by_participant_id_job_participant"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["move_job.id"],
            name=op.f("fk_scope_version_job_id_move_job"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_version_id"],
            ["scope_version.id"],
            name=op.f("fk_scope_version_parent_version_id_scope_version"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scope_version")),
        sa.UniqueConstraint(
            "job_id",
            "sequence_number",
            name="uq_scope_version_job_id_sequence_number",
        ),
        sa.UniqueConstraint("parent_version_id", name=op.f("uq_scope_version_parent_version_id")),
    )
    op.create_index(
        "uq_scope_version_initial_job",
        "scope_version",
        ["job_id"],
        unique=True,
        postgresql_where=sa.text("parent_version_id IS NULL"),
        sqlite_where=sa.text("parent_version_id IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "uq_scope_version_initial_job",
        table_name="scope_version",
        postgresql_where=sa.text("parent_version_id IS NULL"),
        sqlite_where=sa.text("parent_version_id IS NULL"),
    )
    op.drop_table("scope_version")
