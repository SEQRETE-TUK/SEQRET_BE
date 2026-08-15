"""persist completion submission, review request, and customer decision lifecycle

Revision ID: int_04_0001
Revises: a_13_0001
Create Date: 2026-08-15 00:00:00.000000

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "int_04_0001"
down_revision: str | Sequence[str] | None = "a_13_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_COMPLETION_CHECK_ITEMS = (
    '[{"key":"tools_removed","label":"작업 도구와 자재 회수"},'
    '{"key":"site_restored","label":"출발지와 도착지 정리"},'
    '{"key":"changes_recorded","label":"변경·이슈 기록 확인"}]'
)
_EVENTS_BEFORE = (
    "'CAPTURE_SUBMITTED_V1', 'ANALYSIS_COMPLETED_V1', "
    "'ANALYSIS_FAILED_V1', 'SCOPE_LOCKED_V1', 'CHANGE_REQUESTED_V1', "
    "'DISPATCH_CONFIRMED_V1', 'COMPLETION_MEDIA_SUBMITTED_V1', 'MEDIA_DELETED_V1'"
)
_EVENTS_AFTER = (
    "'CAPTURE_SUBMITTED_V1', 'ANALYSIS_COMPLETED_V1', "
    "'ANALYSIS_FAILED_V1', 'SCOPE_LOCKED_V1', 'CHANGE_REQUESTED_V1', "
    "'DISPATCH_CONFIRMED_V1', 'COMPLETION_MEDIA_SUBMITTED_V1', "
    "'COMPLETION_SUBMITTED_V1', 'COMPLETION_REQUESTED_V1', "
    "'COMPLETION_DECIDED_V1', 'MEDIA_DELETED_V1'"
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
    """Add immutable field completion and expiring customer review records."""

    _replace_event_check("outbox_event", "outbox_event_type", _EVENTS_AFTER)
    _replace_event_check(
        "notification_delivery",
        "notification_event_type",
        _EVENTS_AFTER,
    )
    op.add_column(
        "dispatch_setup",
        sa.Column(
            "completion_check_items",
            sa.JSON(),
            nullable=False,
            server_default=sa.text(f"'{_DEFAULT_COMPLETION_CHECK_ITEMS}'"),
        ),
    )
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("dispatch_setup") as batch:
            batch.alter_column(
                "completion_check_items",
                existing_type=sa.JSON(),
                existing_nullable=False,
                server_default=None,
            )
    else:
        op.alter_column(
            "dispatch_setup",
            "completion_check_items",
            existing_type=sa.JSON(),
            existing_nullable=False,
            server_default=None,
        )
    op.create_table(
        "completion_submission",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("client_reference", sa.Uuid(), nullable=False),
        sa.Column("dispatch_plan_id", sa.Uuid(), nullable=False),
        sa.Column("scope_version_id", sa.Uuid(), nullable=False),
        sa.Column("submitted_by_participant_id", sa.Uuid(), nullable=False),
        sa.Column("command_hash", sa.String(length=64), nullable=False),
        sa.Column("completed_check_keys", sa.JSON(), nullable=False),
        sa.Column("worker_shifts", sa.JSON(), nullable=False),
        sa.Column("onsite_customer_confirmed", sa.Boolean(), nullable=False),
        sa.Column("onsite_confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("work_ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(command_hash) = 64", name="command_hash_length"),
        sa.CheckConstraint(
            "onsite_customer_confirmed = true",
            name="onsite_customer_confirmed",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["move_job.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["dispatch_plan_id"],
            ["dispatch_plan.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scope_version_id"],
            ["scope_version.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by_participant_id"],
            ["job_participant.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "client_reference"),
    )
    op.create_index(
        "ix_completion_submission_job_submitted",
        "completion_submission",
        ["job_id", "submitted_at", "id"],
    )
    op.create_table(
        "completion_submission_evidence",
        sa.Column("completion_submission_id", sa.Uuid(), nullable=False),
        sa.Column("media_asset_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["completion_submission_id"],
            ["completion_submission.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["media_asset_id"],
            ["media_asset.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("completion_submission_id", "media_asset_id"),
    )
    op.create_table(
        "completion_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("client_reference", sa.Uuid(), nullable=False),
        sa.Column("completion_submission_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_participant_id", sa.Uuid(), nullable=False),
        sa.Column("command_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "REQUESTED",
                "CONFIRMED",
                "ISSUE_REPORTED",
                "REVOKED",
                name="completion_request_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(length=2000), nullable=True),
        sa.Column("decided_by_participant_id", sa.Uuid(), nullable=True),
        sa.Column("decision_hash", sa.String(length=64), nullable=True),
        sa.Column("unrecorded_extra_charge", sa.Boolean(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(command_hash) = 64", name="command_hash_length"),
        sa.CheckConstraint(
            "status IN ('REQUESTED', 'CONFIRMED', 'ISSUE_REPORTED', 'REVOKED')",
            name="completion_request_status",
        ),
        sa.CheckConstraint(
            "expires_at > requested_at",
            name="completion_request_expiry_order",
        ),
        sa.CheckConstraint(
            "(status = 'REVOKED' AND revoked_at IS NOT NULL AND revoke_reason IS NOT NULL) OR "
            "(status <> 'REVOKED' AND revoked_at IS NULL AND revoke_reason IS NULL)",
            name="completion_request_revocation",
        ),
        sa.CheckConstraint(
            "(status IN ('CONFIRMED', 'ISSUE_REPORTED') "
            "AND decided_by_participant_id IS NOT NULL AND decision_hash IS NOT NULL "
            "AND decided_at IS NOT NULL) OR "
            "(status NOT IN ('CONFIRMED', 'ISSUE_REPORTED') "
            "AND decided_by_participant_id IS NULL AND decision_hash IS NULL "
            "AND decided_at IS NULL)",
            name="completion_request_decision",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["move_job.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["completion_submission_id"],
            ["completion_submission.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_participant_id"],
            ["job_participant.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_participant_id"],
            ["job_participant.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "client_reference"),
    )
    op.create_index(
        "ix_completion_request_job_requested",
        "completion_request",
        ["job_id", "requested_at", "id"],
    )
    op.create_table(
        "completion_problem_report",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("completion_request_id", sa.Uuid(), nullable=False),
        sa.Column(
            "problem_type",
            sa.Enum(
                "MISSING_WORK",
                "DAMAGE",
                "AMOUNT",
                "OTHER",
                name="completion_problem_type",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("description", sa.String(length=2000), nullable=False),
        sa.Column("reported_by_participant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "reported_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "problem_type IN ('MISSING_WORK', 'DAMAGE', 'AMOUNT', 'OTHER')",
            name="completion_problem_type",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["move_job.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["completion_request_id"],
            ["completion_request.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reported_by_participant_id"],
            ["job_participant.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("completion_request_id"),
    )
    op.create_index(
        "ix_completion_problem_job_reported",
        "completion_problem_report",
        ["job_id", "reported_at", "id"],
    )


def downgrade() -> None:
    """Refuse to discard completion history or configured completion checklists."""

    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                "LOCK TABLE completion_problem_report, completion_request, "
                "completion_submission_evidence, completion_submission, dispatch_setup, "
                "outbox_event, notification_delivery IN ACCESS EXCLUSIVE MODE"
            )
        )
    has_completion_history = connection.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM completion_submission) "
            "OR EXISTS (SELECT 1 FROM completion_request) "
            "OR EXISTS (SELECT 1 FROM completion_problem_report) "
            "OR EXISTS (SELECT 1 FROM outbox_event WHERE event_type IN "
            "('COMPLETION_SUBMITTED_V1', 'COMPLETION_REQUESTED_V1', "
            "'COMPLETION_DECIDED_V1')) "
            "OR EXISTS (SELECT 1 FROM notification_delivery WHERE event_type IN "
            "('COMPLETION_SUBMITTED_V1', 'COMPLETION_REQUESTED_V1', "
            "'COMPLETION_DECIDED_V1'))"
        )
    )
    default_checklist = json.loads(_DEFAULT_COMPLETION_CHECK_ITEMS)
    has_custom_checklist = False
    for stored in connection.execute(
        sa.text("SELECT completion_check_items FROM dispatch_setup")
    ).scalars():
        decoded = json.loads(stored) if isinstance(stored, str) else stored
        if decoded != default_checklist:
            has_custom_checklist = True
            break
    if has_completion_history or has_custom_checklist:
        raise RuntimeError(
            "INT-04 completion or dispatch checklist history exists; "
            "roll back the application without downgrading the schema"
        )
    op.drop_index("ix_completion_problem_job_reported", table_name="completion_problem_report")
    op.drop_table("completion_problem_report")
    op.drop_index("ix_completion_request_job_requested", table_name="completion_request")
    op.drop_table("completion_request")
    op.drop_table("completion_submission_evidence")
    op.drop_index("ix_completion_submission_job_submitted", table_name="completion_submission")
    op.drop_table("completion_submission")
    op.drop_column("dispatch_setup", "completion_check_items")
    _replace_event_check("outbox_event", "outbox_event_type", _EVENTS_BEFORE)
    _replace_event_check(
        "notification_delivery",
        "notification_event_type",
        _EVENTS_BEFORE,
    )
