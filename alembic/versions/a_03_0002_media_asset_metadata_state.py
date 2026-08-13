"""enforce media asset metadata state

Revision ID: a_03_0002
Revises: a_12_0001
Create Date: 2026-08-13 09:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "a_03_0002"
down_revision: str | Sequence[str] | None = "a_12_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATE_CHECK = (
    "(status = 'PENDING_UPLOAD' AND actual_size_bytes IS NULL "
    "AND sha256_hex IS NULL AND generation IS NULL AND uploaded_at IS NULL) OR "
    "(status <> 'PENDING_UPLOAD' AND actual_size_bytes IS NOT NULL "
    "AND generation IS NOT NULL AND length(trim(generation)) > 0 "
    "AND uploaded_at IS NOT NULL)"
)


def upgrade() -> None:
    """Reject incomplete uploaded metadata and polluted pending rows."""

    with op.batch_alter_table("media_asset") as batch_op:
        batch_op.create_check_constraint("media_asset_metadata_state", _STATE_CHECK)


def downgrade() -> None:
    """Remove the media metadata state invariant."""

    with op.batch_alter_table("media_asset") as batch_op:
        batch_op.drop_constraint("media_asset_metadata_state", type_="check")
