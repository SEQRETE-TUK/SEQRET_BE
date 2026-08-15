"""persist dispatch snapshots and representative field check-ins

Revision ID: a_13_0001
Revises: int_03_0002
Create Date: 2026-08-15 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a_13_0001"
down_revision: str | Sequence[str] | None = "int_03_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENTS_WITHOUT_DISPATCH = (
    "'CAPTURE_SUBMITTED_V1', 'ANALYSIS_COMPLETED_V1', "
    "'ANALYSIS_FAILED_V1', 'SCOPE_LOCKED_V1', 'CHANGE_REQUESTED_V1', "
    "'COMPLETION_MEDIA_SUBMITTED_V1', 'MEDIA_DELETED_V1'"
)
_EVENTS_WITH_DISPATCH = (
    "'CAPTURE_SUBMITTED_V1', 'ANALYSIS_COMPLETED_V1', "
    "'ANALYSIS_FAILED_V1', 'SCOPE_LOCKED_V1', 'CHANGE_REQUESTED_V1', "
    "'DISPATCH_CONFIRMED_V1', 'COMPLETION_MEDIA_SUBMITTED_V1', 'MEDIA_DELETED_V1'"
)


def _replace_event_check(table: str, short_name: str, allowed: str) -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(short_name, type_="check")
            batch.create_check_constraint(short_name, f"event_type IN ({allowed})")
        return
    op.drop_constraint(short_name, table, type_="check")
    op.create_check_constraint(short_name, table, f"event_type IN ({allowed})")


def upgrade() -> None:
    """Add immutable resource snapshots, assignment, check-in, and notification event."""

    _replace_event_check("outbox_event", "outbox_event_type", _EVENTS_WITH_DISPATCH)
    _replace_event_check(
        "notification_delivery",
        "notification_event_type",
        _EVENTS_WITH_DISPATCH,
    )
    op.create_table(
        "dispatch_setup",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("client_reference", sa.Uuid(), nullable=False),
        sa.Column("source_scope_version_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_participant_id", sa.Uuid(), nullable=False),
        sa.Column("command_hash", sa.String(length=64), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_duration_minutes", sa.Integer(), nullable=False),
        sa.Column("required_vehicle_capacity_m2", sa.Integer(), nullable=False),
        sa.Column("required_worker_count", sa.Integer(), nullable=False),
        sa.Column("required_skills", sa.JSON(), nullable=False),
        sa.Column("required_certifications", sa.JSON(), nullable=False),
        sa.Column("check_in_items", sa.JSON(), nullable=False),
        sa.Column("origin_conditions", sa.JSON(), nullable=False),
        sa.Column("safety_notice", sa.String(length=2000), nullable=False),
        sa.Column("vehicle_options", sa.JSON(), nullable=False),
        sa.Column("worker_options", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(command_hash) = 64", name="command_hash_length"),
        sa.CheckConstraint(
            "expected_duration_minutes BETWEEN 1 AND 720",
            name="duration_range",
        ),
        sa.CheckConstraint(
            "required_vehicle_capacity_m2 >= 0",
            name="vehicle_capacity_nonnegative",
        ),
        sa.CheckConstraint(
            "required_worker_count BETWEEN 1 AND 50",
            name="worker_count_range",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["move_job.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_scope_version_id"],
            ["scope_version.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_participant_id"],
            ["job_participant.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index(
        "ix_dispatch_setup_start",
        "dispatch_setup",
        ["start_at", "job_id"],
    )
    op.create_table(
        "dispatch_plan",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("setup_id", sa.Uuid(), nullable=False),
        sa.Column("source_scope_version_id", sa.Uuid(), nullable=False),
        sa.Column("vehicle_option_id", sa.Uuid(), nullable=False),
        sa.Column("lead_worker_option_id", sa.Uuid(), nullable=False),
        sa.Column("selected_worker_option_ids", sa.JSON(), nullable=False),
        sa.Column("worker_note", sa.String(length=2000), nullable=True),
        sa.Column("command_hash", sa.String(length=64), nullable=False),
        sa.Column("confirmed_by_participant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "confirmed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(command_hash) = 64", name="command_hash_length"),
        sa.ForeignKeyConstraint(["job_id"], ["move_job.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["setup_id"],
            ["dispatch_setup.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_scope_version_id"],
            ["scope_version.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_by_participant_id"],
            ["job_participant.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
        sa.UniqueConstraint("setup_id"),
    )
    op.create_table(
        "field_check_in",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("dispatch_plan_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("worker_option_id", sa.Uuid(), nullable=False),
        sa.Column("confirmed_check_keys", sa.JSON(), nullable=False),
        sa.Column(
            "checked_in_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["move_job.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["dispatch_plan_id"],
            ["dispatch_plan.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"],
            ["job_participant.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "participant_id"),
        sa.UniqueConstraint("dispatch_plan_id", "participant_id"),
    )
    op.create_index(
        "ix_field_check_in_job_checked",
        "field_check_in",
        ["job_id", "checked_in_at", "id"],
    )


def downgrade() -> None:
    """Refuse to discard any dispatch, check-in, or delivery history."""

    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                "LOCK TABLE field_check_in, dispatch_plan, dispatch_setup, "
                "outbox_event, notification_delivery IN ACCESS EXCLUSIVE MODE"
            )
        )
    has_history = connection.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM dispatch_setup) "
            "OR EXISTS (SELECT 1 FROM outbox_event "
            "WHERE event_type = 'DISPATCH_CONFIRMED_V1') "
            "OR EXISTS (SELECT 1 FROM notification_delivery "
            "WHERE event_type = 'DISPATCH_CONFIRMED_V1')"
        )
    )
    if has_history:
        raise RuntimeError(
            "A-13 dispatch or check-in history exists; "
            "roll back the application without downgrading the schema"
        )
    op.drop_index("ix_field_check_in_job_checked", table_name="field_check_in")
    op.drop_table("field_check_in")
    op.drop_table("dispatch_plan")
    op.drop_index("ix_dispatch_setup_start", table_name="dispatch_setup")
    op.drop_table("dispatch_setup")
    _replace_event_check("outbox_event", "outbox_event_type", _EVENTS_WITHOUT_DISPATCH)
    _replace_event_check(
        "notification_delivery",
        "notification_event_type",
        _EVENTS_WITHOUT_DISPATCH,
    )
