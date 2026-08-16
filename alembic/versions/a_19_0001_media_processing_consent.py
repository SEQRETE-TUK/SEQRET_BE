"""record explicit media processing consent snapshots

Revision ID: a_19_0001
Revises: a_16_0001
Create Date: 2026-08-17 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a_19_0001"
down_revision: str | Sequence[str] | None = "a_16_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSENT_CHECK = (
    "(privacy_notice_acknowledged = false "
    "AND media_consent_policy_version IS NULL "
    "AND media_retention_days IS NULL "
    "AND media_consented_at IS NULL) OR "
    "(privacy_notice_acknowledged = true "
    "AND media_consent_policy_version IS NOT NULL "
    "AND length(trim(media_consent_policy_version)) > 0 "
    "AND media_retention_days IS NOT NULL "
    "AND media_retention_days > 0 "
    "AND media_consented_at IS NOT NULL)"
)


def upgrade() -> None:
    """Mark old sessions as unrecorded and require complete future snapshots."""

    op.add_column(
        "capture_session",
        sa.Column("media_consent_policy_version", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "capture_session",
        sa.Column(
            "privacy_notice_acknowledged",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "capture_session",
        sa.Column("media_retention_days", sa.Integer(), nullable=True),
    )
    op.add_column(
        "capture_session",
        sa.Column("media_consented_at", sa.DateTime(timezone=True), nullable=True),
    )

    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("capture_session") as batch:
            batch.alter_column(
                "privacy_notice_acknowledged",
                existing_type=sa.Boolean(),
                existing_nullable=False,
                server_default=None,
            )
            batch.create_check_constraint("capture_media_consent_snapshot", _CONSENT_CHECK)
    else:
        op.alter_column(
            "capture_session",
            "privacy_notice_acknowledged",
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=None,
        )
        op.create_check_constraint(
            "capture_media_consent_snapshot",
            "capture_session",
            _CONSENT_CHECK,
        )


def downgrade() -> None:
    """Remove capture-level consent snapshots."""

    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("capture_session") as batch:
            batch.drop_constraint("capture_media_consent_snapshot", type_="check")
            batch.drop_column("media_consented_at")
            batch.drop_column("media_retention_days")
            batch.drop_column("privacy_notice_acknowledged")
            batch.drop_column("media_consent_policy_version")
    else:
        op.drop_constraint(
            "capture_media_consent_snapshot",
            "capture_session",
            type_="check",
        )
        op.drop_column("capture_session", "media_consented_at")
        op.drop_column("capture_session", "media_retention_days")
        op.drop_column("capture_session", "privacy_notice_acknowledged")
        op.drop_column("capture_session", "media_consent_policy_version")
