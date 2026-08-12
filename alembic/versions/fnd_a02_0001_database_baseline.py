"""database baseline

Revision ID: fnd_a02_0001
Revises:
Create Date: 2026-08-12 09:42:15.657504

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "fnd_a02_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
