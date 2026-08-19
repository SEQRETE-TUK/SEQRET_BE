"""add recoverable move details and ladder condition

Revision ID: int_12_0001
Revises: int_09_0001
Create Date: 2026-08-19 23:30:00.000000

"""

import hashlib
import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "int_12_0001"
down_revision: str | Sequence[str] | None = "int_09_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rewrite_scope_content_ladder(
    content: dict[str, Any],
    *,
    remove: bool,
) -> tuple[dict[str, Any], bool]:
    """Add or remove the condition in one stored scope-shaped document."""

    document = dict(content)
    snapshots = document.get("location_conditions")
    if not isinstance(snapshots, list):
        return document, False

    changed = False
    rewritten_snapshots: list[object] = []
    for stored_snapshot in snapshots:
        if not isinstance(stored_snapshot, dict):
            rewritten_snapshots.append(stored_snapshot)
            continue
        snapshot = dict(stored_snapshot)
        stored_conditions = snapshot.get("conditions")
        if not isinstance(stored_conditions, dict):
            rewritten_snapshots.append(snapshot)
            continue
        conditions = dict(stored_conditions)
        if remove:
            if "ladder" in conditions:
                conditions.pop("ladder")
                changed = True
        elif "ladder" not in conditions:
            conditions["ladder"] = "unknown"
            changed = True
        snapshot["conditions"] = conditions
        rewritten_snapshots.append(snapshot)

    if changed:
        document["location_conditions"] = rewritten_snapshots
    return document, changed


def _scope_content_hash(content: dict[str, Any]) -> str:
    canonical = json.dumps(
        content,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _rewrite_scope_snapshots(*, remove: bool) -> None:
    """Keep immutable scope JSON and its digest aligned with the public schema."""

    connection = op.get_bind()
    scope_version = sa.table(
        "scope_version",
        sa.column("id", sa.Uuid()),
        sa.column("content", sa.JSON()),
        sa.column("content_hash", sa.String(length=64)),
    )
    scope_rows = (
        connection.execute(sa.select(scope_version.c.id, scope_version.c.content)).mappings().all()
    )
    for row in scope_rows:
        content, changed = _rewrite_scope_content_ladder(
            dict(row["content"] or {}),
            remove=remove,
        )
        if not changed:
            continue
        connection.execute(
            sa.update(scope_version)
            .where(scope_version.c.id == row["id"])
            .values(content=content, content_hash=_scope_content_hash(content))
        )

    change_request = sa.table(
        "change_request",
        sa.column("id", sa.Uuid()),
        sa.column("proposed_content", sa.JSON()),
    )
    change_rows = (
        connection.execute(sa.select(change_request.c.id, change_request.c.proposed_content))
        .mappings()
        .all()
    )
    for row in change_rows:
        content, changed = _rewrite_scope_content_ladder(
            dict(row["proposed_content"] or {}),
            remove=remove,
        )
        if changed:
            connection.execute(
                sa.update(change_request)
                .where(change_request.c.id == row["id"])
                .values(proposed_content=content)
            )


def _rewrite_ladder_condition(*, remove: bool) -> None:
    """Keep legacy JSON rows explicit without relying on one database JSON dialect."""

    connection = op.get_bind()
    location = sa.table(
        "location",
        sa.column("id", sa.Uuid()),
        sa.column("conditions", sa.JSON()),
    )
    rows = connection.execute(sa.select(location.c.id, location.c.conditions)).mappings().all()
    for row in rows:
        conditions = dict(row["conditions"] or {})
        if remove:
            if "ladder" not in conditions:
                continue
            conditions.pop("ladder")
        elif "ladder" in conditions:
            continue
        else:
            conditions["ladder"] = "unknown"
        connection.execute(
            sa.update(location).where(location.c.id == row["id"]).values(conditions=conditions)
        )


def _move_detail_history_exists() -> bool:
    connection = op.get_bind()
    location = sa.table(
        "location",
        sa.column("detail_address", sa.String(length=200)),
        sa.column("conditions", sa.JSON()),
    )
    rows = connection.execute(
        sa.select(location.c.detail_address, location.c.conditions)
    ).mappings()
    if any(
        row["detail_address"] is not None
        or dict(row["conditions"] or {}).get("ladder") not in {None, "unknown"}
        for row in rows
    ):
        return True

    for table_name, column_name in (
        ("scope_version", "content"),
        ("change_request", "proposed_content"),
    ):
        history = sa.table(table_name, sa.column(column_name, sa.JSON()))
        documents = connection.execute(sa.select(history.c[column_name])).scalars().all()
        for stored_document in documents:
            document = dict(stored_document or {})
            snapshots = document.get("location_conditions")
            if not isinstance(snapshots, list):
                continue
            for snapshot in snapshots:
                if not isinstance(snapshot, dict):
                    continue
                conditions = snapshot.get("conditions")
                if isinstance(conditions, dict) and conditions.get("ladder") not in {
                    None,
                    "unknown",
                }:
                    return True
    return False


def upgrade() -> None:
    """Separate the detailed address and backfill explicit ladder knowledge."""

    op.add_column(
        "location",
        sa.Column("detail_address", sa.String(length=200), nullable=True),
    )
    _rewrite_ladder_condition(remove=False)
    _rewrite_scope_snapshots(remove=False)


def downgrade() -> None:
    """Remove the detailed address and ladder condition contract."""

    if _move_detail_history_exists():
        raise RuntimeError("move detail rows exist")
    _rewrite_scope_snapshots(remove=True)
    _rewrite_ladder_condition(remove=True)
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("location") as batch:
            batch.drop_column("detail_address")
    else:
        op.drop_column("location", "detail_address")
