"""outbox relay notification delivery

Revision ID: a_09_0001
Revises: a_08_0001
Create Date: 2026-08-12 14:53:00.806900

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a_09_0001"
down_revision: str | Sequence[str] | None = "a_08_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "event_consumption",
        sa.Column("consumer_name", sa.String(length=100), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column(
            "consumed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(consumer_name) > 0", name="consumer_name_present"),
        sa.PrimaryKeyConstraint("consumer_name", "event_id", name=op.f("pk_event_consumption")),
    )
    op.create_table(
        "outbox_event",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column(
            "event_type",
            sa.Enum(
                "CAPTURE_SUBMITTED_V1",
                "ANALYSIS_COMPLETED_V1",
                "ANALYSIS_FAILED_V1",
                "SCOPE_LOCKED_V1",
                "CHANGE_REQUESTED_V1",
                "COMPLETION_MEDIA_SUBMITTED_V1",
                "MEDIA_DELETED_V1",
                name="domain_event_type",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("trace_id", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("lock_token", sa.Uuid(), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "event_type IN ('CAPTURE_SUBMITTED_V1', 'ANALYSIS_COMPLETED_V1', "
            "'ANALYSIS_FAILED_V1', 'SCOPE_LOCKED_V1', 'CHANGE_REQUESTED_V1', "
            "'COMPLETION_MEDIA_SUBMITTED_V1', 'MEDIA_DELETED_V1')",
            name="outbox_event_type",
        ),
        sa.CheckConstraint(
            "(lock_token IS NULL) = (locked_until IS NULL)", name="outbox_lock_pair"
        ),
        sa.CheckConstraint("attempt_count >= 0", name="outbox_attempt_count_nonnegative"),
        sa.CheckConstraint(
            "last_error_code IS NULL OR length(last_error_code) > 0",
            name="outbox_error_code_present",
        ),
        sa.CheckConstraint("length(trace_id) = 32", name="outbox_trace_id_length"),
        sa.CheckConstraint(
            "published_at IS NULL OR "
            "(lock_token IS NULL AND locked_until IS NULL AND last_error_code IS NULL)",
            name="outbox_published_final",
        ),
        sa.CheckConstraint("schema_version > 0", name="outbox_schema_version_positive"),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_outbox_event")),
    )
    op.create_index(
        "ix_outbox_event_delivery",
        "outbox_event",
        ["published_at", "next_attempt_at", "locked_until", "occurred_at"],
        unique=False,
    )
    op.create_table(
        "notification_delivery",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column(
            "event_type",
            sa.Enum(
                "CAPTURE_SUBMITTED_V1",
                "ANALYSIS_COMPLETED_V1",
                "ANALYSIS_FAILED_V1",
                "SCOPE_LOCKED_V1",
                "CHANGE_REQUESTED_V1",
                "COMPLETION_MEDIA_SUBMITTED_V1",
                "MEDIA_DELETED_V1",
                name="domain_event_type",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_participant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "SENT", "FAILED", name="notification_status", native_enum=False),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "event_type IN ('CAPTURE_SUBMITTED_V1', 'ANALYSIS_COMPLETED_V1', "
            "'ANALYSIS_FAILED_V1', 'SCOPE_LOCKED_V1', 'CHANGE_REQUESTED_V1', "
            "'COMPLETION_MEDIA_SUBMITTED_V1', 'MEDIA_DELETED_V1')",
            name="notification_event_type",
        ),
        sa.CheckConstraint(
            "(status = 'FAILED') = (last_error_code IS NOT NULL)", name="notification_failure_code"
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR length(last_error_code) > 0",
            name="notification_error_code_present",
        ),
        sa.CheckConstraint(
            "(status = 'SENT') = (sent_at IS NOT NULL)", name="notification_sent_time"
        ),
        sa.CheckConstraint(
            "(attempt_count > 0) = (last_attempt_at IS NOT NULL)",
            name="notification_attempt_time",
        ),
        sa.CheckConstraint("status IN ('PENDING', 'SENT', 'FAILED')", name="notification_status"),
        sa.CheckConstraint("attempt_count >= 0", name="notification_attempt_count_nonnegative"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["move_job.id"],
            name=op.f("fk_notification_delivery_job_id_move_job"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_participant_id"],
            ["job_participant.id"],
            name=op.f("fk_notification_delivery_recipient_participant_id_job_participant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_delivery")),
        sa.UniqueConstraint(
            "event_id",
            "recipient_participant_id",
            name=op.f("uq_notification_delivery_event_id"),
        ),
    )
    op.create_index(
        "ix_notification_delivery_recipient_created",
        "notification_delivery",
        ["recipient_participant_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_notification_delivery_recipient_created",
        table_name="notification_delivery",
    )
    op.drop_table("notification_delivery")
    op.drop_index("ix_outbox_event_delivery", table_name="outbox_event")
    op.drop_table("outbox_event")
    op.drop_table("event_consumption")
