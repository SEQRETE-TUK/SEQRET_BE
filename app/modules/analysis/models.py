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
            "result_schema_version IS NULL OR result_schema_version = 1",
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
        Index("ix_detection_analysis_run", "analysis_run_id", "ordinal", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    analysis_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("ai_analysis_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    item_key: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False)
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
