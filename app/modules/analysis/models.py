"""B-owned AI analysis run and detection persistence.

These tables hold provider-neutral draft results only. They never create,
reference, or lock an immutable ``scope_version``; importing a draft into a
scope version stays behind track A's ``ImportAnalysisDraft`` command.
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base


class AnalysisRunStatus(StrEnum):
    """Lifecycle of one asynchronous analysis attempt."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AiAnalysisRun(Base):
    """One derived, human-editable analysis attempt for a capture session."""

    __tablename__ = "ai_analysis_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')",
            name="ai_analysis_run_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ai_analysis_run_attempt_nonnegative"),
        CheckConstraint("length(trace_id) = 32", name="ai_analysis_run_trace_id_length"),
        CheckConstraint(
            "(status = 'PENDING') = (started_at IS NULL)",
            name="ai_analysis_run_started_time",
        ),
        CheckConstraint(
            "(status IN ('COMPLETED', 'FAILED')) = (completed_at IS NOT NULL)",
            name="ai_analysis_run_completion_time",
        ),
        CheckConstraint(
            "(status = 'FAILED') = (failure_code IS NOT NULL)",
            name="ai_analysis_run_failure_code",
        ),
        CheckConstraint(
            "failure_code IS NULL OR length(failure_code) > 0",
            name="ai_analysis_run_failure_code_present",
        ),
        CheckConstraint(
            "failure_stage IS NULL OR length(failure_stage) > 0",
            name="ai_analysis_run_failure_stage_present",
        ),
        CheckConstraint(
            "failure_detail_code IS NULL OR length(failure_detail_code) > 0",
            name="ai_analysis_run_failure_detail_present",
        ),
        CheckConstraint(
            "(status = 'COMPLETED') = (model_name IS NOT NULL)",
            name="ai_analysis_run_completed_model",
        ),
        CheckConstraint(
            "((model_name IS NULL) = (model_version IS NULL)) "
            "AND ((model_name IS NULL) = (prompt_version IS NULL)) "
            "AND ((model_name IS NULL) = (result_schema_version IS NULL))",
            name="ai_analysis_run_result_versions",
        ),
        CheckConstraint(
            "result_schema_version IS NULL OR result_schema_version IN (1, 2)",
            name="ai_analysis_run_result_schema_version",
        ),
        Index(
            "ix_ai_analysis_run_capture_session",
            "capture_session_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    capture_session_id: Mapped[UUID] = mapped_column(
        ForeignKey("capture_session.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[AnalysisRunStatus] = mapped_column(
        Enum(AnalysisRunStatus, name="ai_analysis_run_status", native_enum=False),
        nullable=False,
        default=AnalysisRunStatus.PENDING,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(100))
    model_version: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(100))
    result_schema_version: Mapped[int | None] = mapped_column(Integer)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    failure_stage: Mapped[str | None] = mapped_column(String(32))
    provider_status: Mapped[int | None] = mapped_column(Integer)
    failure_detail_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Detection(Base):
    """One structured draft item a human may edit, keep, or reject."""

    __tablename__ = "detection"
    __table_args__ = (
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="detection_confidence_range",
        ),
        CheckConstraint("length(item_key) > 0", name="detection_item_key_present"),
        CheckConstraint("length(description) > 0", name="detection_description_present"),
        CheckConstraint("ordinal >= 0", name="detection_ordinal_nonnegative"),
        CheckConstraint(
            "(item_schema_version = 1 AND name IS NULL AND quantity IS NULL "
            "AND unit IS NULL AND work_note IS NULL) OR "
            "(item_schema_version = 2 AND name IS NOT NULL "
            "AND length(name) > 0 "
            "AND ((review_required = false AND quantity IS NOT NULL "
            "AND unit IS NOT NULL) OR (review_required = true "
            "AND ((quantity IS NULL AND unit IS NULL) "
            "OR (quantity IS NOT NULL AND unit IS NOT NULL)))) "
            "AND (quantity IS NULL OR quantity >= 1) "
            "AND (unit IS NULL OR length(unit) > 0) "
            "AND (work_note IS NULL OR length(work_note) > 0))",
            name="detection_item_schema_shape",
        ),
        UniqueConstraint(
            "analysis_run_id",
            "ordinal",
            name="uq_detection_analysis_run_ordinal",
        ),
        Index("ix_detection_analysis_run", "analysis_run_id", "ordinal", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    analysis_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("ai_analysis_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    item_schema_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    item_key: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    quantity: Mapped[int | None] = mapped_column(Integer)
    unit: Mapped[str | None] = mapped_column(String(20))
    work_note: Mapped[str | None] = mapped_column(String(500))
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    review_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_media_asset_ids: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class AnalysisLocationConditionSuggestion(Base):
    """One provider-derived location condition that always requires review."""

    __tablename__ = "analysis_location_condition_suggestion"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="analysis_location_suggestion_ordinal_nonnegative"),
        CheckConstraint(
            "location_kind IN ('origin', 'destination')",
            name="analysis_location_suggestion_kind",
        ),
        CheckConstraint(
            "residence_type IN ('apartment', 'villa', 'officetel', 'house', "
            "'studio', 'other', 'unknown')",
            name="analysis_location_suggestion_residence_type",
        ),
        CheckConstraint(
            "floor_status IN ('known', 'unknown') "
            "AND ((floor_status = 'known' AND floor_value IS NOT NULL) "
            "OR (floor_status = 'unknown' AND floor_value IS NULL)) "
            "AND (floor_value IS NULL OR (floor_value >= -10 AND floor_value <= 200))",
            name="analysis_location_suggestion_floor",
        ),
        CheckConstraint(
            "elevator IN ('available', 'unavailable', 'unknown')",
            name="analysis_location_suggestion_elevator",
        ),
        CheckConstraint(
            "stairs IN ('required', 'not_required', 'unknown')",
            name="analysis_location_suggestion_stairs",
        ),
        CheckConstraint(
            "parking_access IN ('available', 'restricted', 'unavailable', 'unknown')",
            name="analysis_location_suggestion_parking",
        ),
        CheckConstraint(
            "carry_distance_status IN ('known', 'unknown') "
            "AND ((carry_distance_status = 'known' "
            "AND carry_distance_value_m IS NOT NULL) "
            "OR (carry_distance_status = 'unknown' "
            "AND carry_distance_value_m IS NULL)) "
            "AND (carry_distance_value_m IS NULL "
            "OR (carry_distance_value_m >= 0 AND carry_distance_value_m <= 100000))",
            name="analysis_location_suggestion_carry_distance",
        ),
        CheckConstraint(
            "access_note IS NULL OR length(access_note) > 0",
            name="analysis_location_suggestion_access_note",
        ),
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="analysis_location_suggestion_confidence",
        ),
        UniqueConstraint(
            "analysis_run_id",
            "ordinal",
            name="uq_analysis_location_suggestion_run_ordinal",
        ),
        UniqueConstraint(
            "analysis_run_id",
            "location_id",
            name="uq_analysis_location_suggestion_run_location",
        ),
        UniqueConstraint(
            "analysis_run_id",
            "location_kind",
            name="uq_analysis_location_suggestion_run_kind",
        ),
        Index(
            "ix_analysis_location_suggestion_run",
            "analysis_run_id",
            "ordinal",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    analysis_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("ai_analysis_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    location_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    location_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    residence_type: Mapped[str] = mapped_column(String(20), nullable=False)
    floor_status: Mapped[str] = mapped_column(String(10), nullable=False)
    floor_value: Mapped[int | None] = mapped_column(Integer)
    elevator: Mapped[str] = mapped_column(String(20), nullable=False)
    stairs: Mapped[str] = mapped_column(String(20), nullable=False)
    parking_access: Mapped[str] = mapped_column(String(20), nullable=False)
    carry_distance_status: Mapped[str] = mapped_column(String(10), nullable=False)
    carry_distance_value_m: Mapped[int | None] = mapped_column(Integer)
    access_note: Mapped[str | None] = mapped_column(String(1000))
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    review_required_fields: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    source_media_asset_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
