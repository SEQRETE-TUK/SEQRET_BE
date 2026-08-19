"""add durable workspaces, editable jobs, and external notification delivery

Revision ID: int_09_0001
Revises: a_23_0001
Create Date: 2026-08-19 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "int_09_0001"
down_revision: str | Sequence[str] | None = "a_23_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUDIT_EVENT_TYPES = (
    "'JOB_CREATED', 'PARTICIPANT_CONNECTED', 'ACCESS_LINK_ISSUED', "
    "'ACCESS_LINK_REVOKED', 'COMPLETION_MEDIA_UPLOADED', "
    "'SCOPE_VERSION_CREATED', 'SCOPE_VERSION_APPROVED', "
    "'SCOPE_VERSION_LOCKED', 'CHANGE_REQUESTED', "
    "'CHANGE_CLARIFICATION_REQUESTED', 'CHANGE_EXPLAINED', "
    "'CHANGE_APPROVED', 'CHANGE_REJECTED', 'COMPLETION_CONFIRMED', "
    "'JOB_COMPLETED'"
)
SQLITE_AUDIT_UPDATE_TRIGGER = "audit_event_prevent_update"
SQLITE_AUDIT_DELETE_TRIGGER = "audit_event_prevent_delete"


def _replace_check(table: str, name: str, expression: str) -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(name, type_="check")
            batch.create_check_constraint(name, expression)
        return
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, expression)


def _restore_sqlite_audit_triggers() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER IF NOT EXISTS {SQLITE_AUDIT_UPDATE_TRIGGER}
            BEFORE UPDATE ON audit_event
            BEGIN
                SELECT RAISE(ABORT, 'audit_event is append-only');
            END
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER IF NOT EXISTS {SQLITE_AUDIT_DELETE_TRIGGER}
            BEFORE DELETE ON audit_event
            BEGIN
                SELECT RAISE(ABORT, 'audit_event is append-only');
            END
            """
        )
    )


def upgrade() -> None:
    """Create account sessions and expand notification intents by channel."""

    op.create_table(
        "workspace_account",
        sa.Column("id", sa.Uuid(), nullable=False),
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
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "role IN ('CUSTOMER', 'COMPANY_MANAGER', 'FIELD_WORKER')",
            name="workspace_account_role",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspace_account"),
    )
    op.create_table(
        "workspace_membership",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["workspace_account.id"],
            name="fk_workspace_membership_account_id_workspace_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"],
            ["job_participant.id"],
            name="fk_workspace_membership_participant_id_job_participant",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspace_membership"),
    )
    op.create_index(
        "uq_workspace_membership_active_participant",
        "workspace_membership",
        ["participant_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
        sqlite_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "ix_workspace_membership_account_joined",
        "workspace_membership",
        ["account_id", "joined_at", "id"],
        unique=False,
    )
    op.create_table(
        "workspace_session",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_token", sa.String(length=100), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "length(token_hash) = 64",
            name="workspace_session_token_hash_length",
        ),
        sa.CheckConstraint(
            "length(csrf_token) >= 40",
            name="workspace_session_csrf_token_length",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="workspace_session_expiry_after_creation",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["workspace_account.id"],
            name="fk_workspace_session_account_id_workspace_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspace_session"),
        sa.UniqueConstraint("token_hash", name="uq_workspace_session_token_hash"),
    )
    op.create_index(
        "ix_workspace_session_account_expiry",
        "workspace_session",
        ["account_id", "expires_at", "id"],
        unique=False,
    )
    op.create_table(
        "workspace_contact_point",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column(
            "channel",
            sa.Enum(
                "EMAIL",
                "SMS",
                "KAKAO",
                name="notification_contact_channel",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("destination", sa.String(length=320), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "channel IN ('EMAIL', 'SMS', 'KAKAO')",
            name="workspace_contact_point_channel",
        ),
        sa.CheckConstraint(
            "length(destination) > 0",
            name="workspace_contact_point_destination",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["workspace_account.id"],
            name="fk_workspace_contact_point_account_id_workspace_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspace_contact_point"),
        sa.UniqueConstraint(
            "account_id",
            "channel",
            name="uq_workspace_contact_point_account_id",
        ),
    )

    _replace_check(
        "audit_event",
        "audit_event_type",
        f"event_type IN ({AUDIT_EVENT_TYPES}, 'JOB_BASIC_INFO_UPDATED')",
    )
    _restore_sqlite_audit_triggers()

    with op.batch_alter_table("notification_delivery") as batch:
        batch.add_column(
            sa.Column(
                "channel",
                sa.Enum(
                    "IN_APP",
                    "EMAIL",
                    "SMS",
                    "KAKAO",
                    name="notification_channel",
                    native_enum=False,
                ),
                server_default="IN_APP",
                nullable=False,
            )
        )
        batch.add_column(sa.Column("destination", sa.String(length=320), nullable=True))
        batch.add_column(sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("lock_token", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("provider_message_id", sa.String(length=255), nullable=True))
        batch.drop_constraint("uq_notification_delivery_event_id", type_="unique")
        batch.create_unique_constraint(
            "uq_notification_delivery_event_recipient_channel",
            ["event_id", "recipient_participant_id", "channel"],
        )
        batch.create_check_constraint(
            "notification_channel",
            "channel IN ('IN_APP', 'EMAIL', 'SMS', 'KAKAO')",
        )
        batch.create_check_constraint(
            "notification_destination_by_channel",
            "(channel = 'IN_APP' AND destination IS NULL) OR "
            "(channel <> 'IN_APP' AND destination IS NOT NULL)",
        )
    op.create_index(
        "ix_notification_delivery_pending",
        "notification_delivery",
        ["status", "next_attempt_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Refuse to discard workspace, edit-audit, or external-delivery history."""

    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("LOCK TABLE workspace_account IN ACCESS EXCLUSIVE MODE"))
        connection.execute(sa.text("LOCK TABLE notification_delivery IN ACCESS EXCLUSIVE MODE"))
        connection.execute(sa.text("LOCK TABLE audit_event IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT 1 FROM workspace_account LIMIT 1")):
        raise RuntimeError(
            "workspace rows exist; roll back the application without downgrading the schema"
        )
    if connection.scalar(
        sa.text("SELECT 1 FROM audit_event WHERE event_type = 'JOB_BASIC_INFO_UPDATED' LIMIT 1")
    ):
        raise RuntimeError(
            "job edit audit rows exist; roll back the application without downgrading the schema"
        )
    if connection.scalar(
        sa.text(
            "SELECT 1 FROM notification_delivery "
            "WHERE channel <> 'IN_APP' OR destination IS NOT NULL "
            "OR provider_message_id IS NOT NULL LIMIT 1"
        )
    ):
        raise RuntimeError(
            "external notification rows exist; roll back the application without downgrading the schema"
        )

    op.drop_index("ix_notification_delivery_pending", table_name="notification_delivery")
    with op.batch_alter_table("notification_delivery") as batch:
        batch.drop_constraint(
            "notification_destination_by_channel",
            type_="check",
        )
        batch.drop_constraint("notification_channel", type_="check")
        batch.drop_constraint(
            "uq_notification_delivery_event_recipient_channel",
            type_="unique",
        )
        batch.create_unique_constraint(
            "uq_notification_delivery_event_id",
            ["event_id", "recipient_participant_id"],
        )
        batch.drop_column("provider_message_id")
        batch.drop_column("lock_token")
        batch.drop_column("locked_until")
        batch.drop_column("next_attempt_at")
        batch.drop_column("destination")
        batch.drop_column("channel")

    _replace_check(
        "audit_event",
        "audit_event_type",
        f"event_type IN ({AUDIT_EVENT_TYPES})",
    )
    _restore_sqlite_audit_triggers()
    op.drop_table("workspace_contact_point")
    op.drop_index("ix_workspace_session_account_expiry", table_name="workspace_session")
    op.drop_table("workspace_session")
    op.drop_index("ix_workspace_membership_account_joined", table_name="workspace_membership")
    op.drop_table("workspace_membership")
    op.drop_table("workspace_account")
