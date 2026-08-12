"""move job participant location room zone

Revision ID: a_01_0001
Revises: fnd_a02_0001
Create Date: 2026-08-12 10:32:39.559589

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a_01_0001"
down_revision: str | Sequence[str] | None = "fnd_a02_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""

    op.create_table(
        "move_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "DRAFT",
                "ACTIVE",
                "COMPLETED",
                "CANCELED",
                name="move_job_status",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'ACTIVE', 'COMPLETED', 'CANCELED')",
            name="move_job_status",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_move_job")),
    )
    op.create_table(
        "job_participant",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
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
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('CUSTOMER', 'COMPANY_MANAGER', 'FIELD_WORKER')",
            name="participant_role",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["move_job.id"],
            name=op.f("fk_job_participant_job_id_move_job"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_participant")),
        sa.UniqueConstraint("job_id", "role", name=op.f("uq_job_participant_job_id")),
    )
    op.create_table(
        "location",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "ORIGIN",
                "DESTINATION",
                name="location_kind",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('ORIGIN', 'DESTINATION')",
            name="location_kind",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["move_job.id"],
            name=op.f("fk_location_job_id_move_job"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_location")),
        sa.UniqueConstraint("job_id", "kind", name=op.f("uq_location_job_id")),
    )
    op.create_table(
        "room_zone",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("location_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "sort_order >= 0",
            name=op.f("ck_room_zone_sort_order_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["location.id"],
            name=op.f("fk_room_zone_location_id_location"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_room_zone")),
        sa.UniqueConstraint("location_id", "name", name=op.f("uq_room_zone_location_id")),
        sa.UniqueConstraint(
            "location_id", "sort_order", name="uq_room_zone_location_id_sort_order"
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""

    op.drop_table("room_zone")
    op.drop_table("location")
    op.drop_table("job_participant")
    op.drop_table("move_job")
