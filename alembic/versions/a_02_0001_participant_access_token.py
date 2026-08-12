"""participant access token

Revision ID: a_02_0001
Revises: a_01_0001
Create Date: 2026-08-12 11:11:45.272468

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a_02_0001"
down_revision: str | Sequence[str] | None = "a_01_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "participant_access_token",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        sa.CheckConstraint("length(token_hash) = 64", name="token_hash_length"),
        sa.ForeignKeyConstraint(
            ["participant_id"],
            ["job_participant.id"],
            name=op.f("fk_participant_access_token_participant_id_job_participant"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_participant_access_token")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_participant_access_token_token_hash")),
    )
    op.create_index(
        "uq_participant_access_token_active_participant",
        "participant_access_token",
        ["participant_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
        sqlite_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "uq_participant_access_token_active_participant",
        table_name="participant_access_token",
        postgresql_where=sa.text("revoked_at IS NULL"),
        sqlite_where=sa.text("revoked_at IS NULL"),
    )
    op.drop_table("participant_access_token")
