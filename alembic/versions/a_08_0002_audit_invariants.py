"""enforce append-only audit and guarded history rollback

Revision ID: a_08_0002
Revises: a_09_0003
Create Date: 2026-08-13 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a_08_0002"
down_revision: str | Sequence[str] | None = "a_09_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POSTGRESQL_FUNCTION = "seqret_reject_audit_event_mutation"
_POSTGRESQL_TRIGGER = "audit_event_append_only"
_SQLITE_UPDATE_TRIGGER = "audit_event_prevent_update"
_SQLITE_DELETE_TRIGGER = "audit_event_prevent_delete"


def upgrade() -> None:
    """Reject mutations to persisted audit facts at the database boundary."""

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            sa.text(
                f"""
                CREATE FUNCTION {_POSTGRESQL_FUNCTION}()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $function$
                BEGIN
                    RAISE EXCEPTION 'audit_event is append-only' USING ERRCODE = '55000';
                END;
                $function$
                """
            )
        )
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {_POSTGRESQL_TRIGGER}
                BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_event
                FOR EACH STATEMENT
                EXECUTE FUNCTION {_POSTGRESQL_FUNCTION}()
                """
            )
        )
        return
    if dialect == "sqlite":
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {_SQLITE_UPDATE_TRIGGER}
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
                CREATE TRIGGER {_SQLITE_DELETE_TRIGGER}
                BEFORE DELETE ON audit_event
                BEGIN
                    SELECT RAISE(ABORT, 'audit_event is append-only');
                END
                """
            )
        )
        return
    raise RuntimeError(f"audit append-only enforcement is unsupported for {dialect}")


def downgrade() -> None:
    """Refuse to discard audit or completion history."""

    connection = op.get_bind()
    dialect = connection.dialect.name
    if dialect == "postgresql":
        connection.execute(
            sa.text(
                "LOCK TABLE completion_confirmation, completion_evidence, audit_event "
                "IN ACCESS EXCLUSIVE MODE"
            )
        )
    if connection.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM audit_event) "
            "OR EXISTS (SELECT 1 FROM completion_confirmation) "
            "OR EXISTS (SELECT 1 FROM completion_evidence)"
        )
    ):
        raise RuntimeError(
            "audit or completion history exists; "
            "roll back the application without downgrading the schema"
        )

    if dialect == "postgresql":
        op.execute(sa.text(f"DROP TRIGGER {_POSTGRESQL_TRIGGER} ON audit_event"))
        op.execute(sa.text(f"DROP FUNCTION {_POSTGRESQL_FUNCTION}()"))
        return
    if dialect == "sqlite":
        op.execute(sa.text(f"DROP TRIGGER {_SQLITE_UPDATE_TRIGGER}"))
        op.execute(sa.text(f"DROP TRIGGER {_SQLITE_DELETE_TRIGGER}"))
        return
    raise RuntimeError(f"audit append-only enforcement is unsupported for {dialect}")
