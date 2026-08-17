"""persist structured AI result v2

Revision ID: b_08_0001
Revises: a_19_0001
Create Date: 2026-08-17 09:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b_08_0001"
down_revision: str | Sequence[str] | None = "a_19_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ITEM_SHAPE = (
    "(item_schema_version = 1 AND name IS NULL AND quantity IS NULL "
    "AND unit IS NULL AND work_note IS NULL) OR "
    "(item_schema_version = 2 AND name IS NOT NULL "
    "AND length(name) > 0 "
    "AND ((review_required = false AND quantity IS NOT NULL AND unit IS NOT NULL) "
    "OR (review_required = true AND ((quantity IS NULL AND unit IS NULL) "
    "OR (quantity IS NOT NULL AND unit IS NOT NULL)))) "
    "AND (quantity IS NULL OR quantity >= 1) "
    "AND (unit IS NULL OR length(unit) > 0) "
    "AND (work_note IS NULL OR length(work_note) > 0))"
)


def _replace_result_version_constraint(*, versions: str) -> None:
    condition = f"result_schema_version IS NULL OR result_schema_version IN ({versions})"
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("ai_analysis_run") as batch:
            batch.drop_constraint("ai_analysis_run_result_schema_version", type_="check")
            batch.create_check_constraint("ai_analysis_run_result_schema_version", condition)
    else:
        op.drop_constraint(
            "ai_analysis_run_result_schema_version",
            "ai_analysis_run",
            type_="check",
        )
        op.create_check_constraint(
            "ai_analysis_run_result_schema_version",
            "ai_analysis_run",
            condition,
        )


def upgrade() -> None:
    """Add v2 item fields and review-only location condition suggestions."""

    _replace_result_version_constraint(versions="1, 2")

    with op.batch_alter_table("detection") as batch:
        batch.add_column(
            sa.Column(
                "item_schema_version",
                sa.Integer(),
                server_default=sa.text("1"),
                nullable=False,
            )
        )
        batch.add_column(sa.Column("name", sa.String(length=200), nullable=True))
        batch.add_column(sa.Column("quantity", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("unit", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("work_note", sa.String(length=500), nullable=True))
        batch.create_check_constraint("detection_item_schema_shape", _ITEM_SHAPE)
        batch.create_unique_constraint(
            "uq_detection_analysis_run_ordinal",
            ["analysis_run_id", "ordinal"],
        )

    op.create_table(
        "analysis_location_condition_suggestion",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Uuid(), nullable=False),
        sa.Column("location_kind", sa.String(length=20), nullable=False),
        sa.Column("residence_type", sa.String(length=20), nullable=False),
        sa.Column("floor_status", sa.String(length=10), nullable=False),
        sa.Column("floor_value", sa.Integer(), nullable=True),
        sa.Column("elevator", sa.String(length=20), nullable=False),
        sa.Column("stairs", sa.String(length=20), nullable=False),
        sa.Column("parking_access", sa.String(length=20), nullable=False),
        sa.Column("carry_distance_status", sa.String(length=10), nullable=False),
        sa.Column("carry_distance_value_m", sa.Integer(), nullable=True),
        sa.Column("access_note", sa.String(length=1000), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("review_required_fields", sa.JSON(), nullable=False),
        sa.Column("source_media_asset_ids", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="analysis_location_suggestion_ordinal_nonnegative",
        ),
        sa.CheckConstraint(
            "location_kind IN ('origin', 'destination')",
            name="analysis_location_suggestion_kind",
        ),
        sa.CheckConstraint(
            "residence_type IN ('apartment', 'villa', 'officetel', 'house', "
            "'studio', 'other', 'unknown')",
            name="analysis_location_suggestion_residence_type",
        ),
        sa.CheckConstraint(
            "floor_status IN ('known', 'unknown') "
            "AND ((floor_status = 'known' AND floor_value IS NOT NULL) "
            "OR (floor_status = 'unknown' AND floor_value IS NULL)) "
            "AND (floor_value IS NULL OR (floor_value >= -10 AND floor_value <= 200))",
            name="analysis_location_suggestion_floor",
        ),
        sa.CheckConstraint(
            "elevator IN ('available', 'unavailable', 'unknown')",
            name="analysis_location_suggestion_elevator",
        ),
        sa.CheckConstraint(
            "stairs IN ('required', 'not_required', 'unknown')",
            name="analysis_location_suggestion_stairs",
        ),
        sa.CheckConstraint(
            "parking_access IN ('available', 'restricted', 'unavailable', 'unknown')",
            name="analysis_location_suggestion_parking",
        ),
        sa.CheckConstraint(
            "carry_distance_status IN ('known', 'unknown') "
            "AND ((carry_distance_status = 'known' AND carry_distance_value_m IS NOT NULL) "
            "OR (carry_distance_status = 'unknown' AND carry_distance_value_m IS NULL)) "
            "AND (carry_distance_value_m IS NULL "
            "OR (carry_distance_value_m >= 0 AND carry_distance_value_m <= 100000))",
            name="analysis_location_suggestion_carry_distance",
        ),
        sa.CheckConstraint(
            "access_note IS NULL OR length(access_note) > 0",
            name="analysis_location_suggestion_access_note",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="analysis_location_suggestion_confidence",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["ai_analysis_run.id"],
            name=op.f("fk_analysis_location_condition_suggestion_analysis_run_id_ai_analysis_run"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_analysis_location_condition_suggestion"),
        ),
        sa.UniqueConstraint(
            "analysis_run_id",
            "ordinal",
            name="uq_analysis_location_suggestion_run_ordinal",
        ),
        sa.UniqueConstraint(
            "analysis_run_id",
            "location_id",
            name="uq_analysis_location_suggestion_run_location",
        ),
        sa.UniqueConstraint(
            "analysis_run_id",
            "location_kind",
            name="uq_analysis_location_suggestion_run_kind",
        ),
    )
    op.create_index(
        "ix_analysis_location_suggestion_run",
        "analysis_location_condition_suggestion",
        ["analysis_run_id", "ordinal", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove v2 storage only when no v2-derived history would be lost."""

    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                "LOCK TABLE ai_analysis_run, detection, "
                "analysis_location_condition_suggestion IN ACCESS EXCLUSIVE MODE"
            )
        )
    v2_rows_exist = (
        connection.scalar(
            sa.text("SELECT 1 FROM ai_analysis_run WHERE result_schema_version = 2 LIMIT 1")
        )
        or connection.scalar(
            sa.text("SELECT 1 FROM analysis_location_condition_suggestion LIMIT 1")
        )
        or connection.scalar(
            sa.text("SELECT 1 FROM detection WHERE item_schema_version = 2 LIMIT 1")
        )
    )
    if v2_rows_exist:
        raise RuntimeError(
            "AI result v2 rows exist; roll back the application without downgrading the schema"
        )

    op.drop_index(
        "ix_analysis_location_suggestion_run",
        table_name="analysis_location_condition_suggestion",
    )
    op.drop_table("analysis_location_condition_suggestion")

    with op.batch_alter_table("detection") as batch:
        batch.drop_constraint("uq_detection_analysis_run_ordinal", type_="unique")
        batch.drop_constraint("detection_item_schema_shape", type_="check")
        batch.drop_column("work_note")
        batch.drop_column("unit")
        batch.drop_column("quantity")
        batch.drop_column("name")
        batch.drop_column("item_schema_version")

    _replace_result_version_constraint(versions="1")
