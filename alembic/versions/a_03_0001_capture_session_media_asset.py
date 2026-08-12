"""capture session media asset

Revision ID: a_03_0001
Revises: a_02_0001
Create Date: 2026-08-12 11:29:24.789498

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a_03_0001"
down_revision: str | Sequence[str] | None = "a_02_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "capture_session",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_participant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by_participant_id"],
            ["job_participant.id"],
            name=op.f("fk_capture_session_created_by_participant_id_job_participant"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["move_job.id"],
            name=op.f("fk_capture_session_job_id_move_job"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_capture_session")),
    )
    op.create_index(
        op.f("ix_capture_session_job_id"),
        "capture_session",
        ["job_id"],
        unique=False,
    )
    op.create_table(
        "media_asset",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("capture_session_id", sa.Uuid(), nullable=False),
        sa.Column("room_zone_id", sa.Uuid(), nullable=False),
        sa.Column(
            "media_purpose",
            sa.Enum(
                "INVENTORY",
                "CONDITION",
                "CHANGE_EVIDENCE",
                "COMPLETION",
                name="media_purpose",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING_UPLOAD",
                "UPLOADED",
                "PROCESSING",
                "READY",
                "FAILED",
                "DELETED",
                name="media_asset_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("expected_size_bytes", sa.Integer(), nullable=False),
        sa.Column("actual_size_bytes", sa.Integer(), nullable=True),
        sa.Column("sha256_hex", sa.String(length=64), nullable=True),
        sa.Column("generation", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "content_type IN ('image/jpeg', 'image/png', 'video/mp4')", name="content_type_allowed"
        ),
        sa.CheckConstraint(
            "content_type NOT LIKE 'image/%%' OR expected_size_bytes <= 20971520",
            name="image_size_within_limit",
        ),
        sa.CheckConstraint(
            "media_purpose IN ('INVENTORY', 'CONDITION', 'CHANGE_EVIDENCE', 'COMPLETION')",
            name="media_purpose",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING_UPLOAD', 'UPLOADED', 'PROCESSING', 'READY', 'FAILED', 'DELETED')",
            name="media_asset_status",
        ),
        sa.CheckConstraint(
            "actual_size_bytes IS NULL OR actual_size_bytes >= 0", name="actual_size_nonnegative"
        ),
        sa.CheckConstraint(
            "expected_size_bytes > 0 AND expected_size_bytes <= 209715200",
            name="expected_size_within_limit",
        ),
        sa.CheckConstraint("sha256_hex IS NULL OR length(sha256_hex) = 64", name="sha256_length"),
        sa.ForeignKeyConstraint(
            ["capture_session_id"],
            ["capture_session.id"],
            name=op.f("fk_media_asset_capture_session_id_capture_session"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["room_zone_id"],
            ["room_zone.id"],
            name=op.f("fk_media_asset_room_zone_id_room_zone"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_asset")),
        sa.UniqueConstraint("object_key", name=op.f("uq_media_asset_object_key")),
    )
    op.create_index(
        op.f("ix_media_asset_capture_session_id"),
        "media_asset",
        ["capture_session_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_media_asset_capture_session_id"), table_name="media_asset")
    op.drop_table("media_asset")
    op.drop_index(op.f("ix_capture_session_job_id"), table_name="capture_session")
    op.drop_table("capture_session")
