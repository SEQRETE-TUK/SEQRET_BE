"""A-owned durable state for capture submission and analysis orchestration."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
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


class CaptureAnalysisStatus(StrEnum):
    """Lifecycle from participant submission to an editable scope draft."""

    PENDING = "pending"
    DISPATCHING = "dispatching"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class CaptureAnalysisDispatch(Base):
    """Exactly one durable analysis orchestration intent per capture session."""

    __tablename__ = "capture_analysis_dispatch"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'DISPATCHING', 'QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')",
            name="capture_analysis_dispatch_status",
        ),
        CheckConstraint(
            "dispatch_attempt_count >= 0",
            name="capture_analysis_dispatch_attempt_nonnegative",
        ),
        CheckConstraint(
            "length(trace_id) = 32",
            name="capture_analysis_dispatch_trace_id_length",
        ),
        CheckConstraint(
            "(dispatch_attempt_count = 0) = (last_attempt_at IS NULL)",
            name="capture_analysis_dispatch_attempt_time",
        ),
        CheckConstraint(
            "(status = 'DISPATCHING' AND dispatch_token IS NOT NULL "
            "AND dispatch_locked_until IS NOT NULL) OR "
            "(status <> 'DISPATCHING' AND dispatch_token IS NULL "
            "AND dispatch_locked_until IS NULL)",
            name="capture_analysis_dispatch_lease",
        ),
        CheckConstraint(
            "last_dispatch_error_code IS NULL OR length(last_dispatch_error_code) > 0",
            name="capture_analysis_dispatch_error_present",
        ),
        CheckConstraint(
            "(status IN ('COMPLETED', 'FAILED')) = (completed_at IS NOT NULL)",
            name="capture_analysis_dispatch_completion_time",
        ),
        CheckConstraint(
            "(status = 'COMPLETED') = (scope_version_id IS NOT NULL)",
            name="capture_analysis_dispatch_scope_version",
        ),
        CheckConstraint(
            "(status = 'FAILED' AND failure_code IS NOT NULL AND retryable IS NOT NULL) OR "
            "(status <> 'FAILED' AND failure_code IS NULL AND retryable IS NULL)",
            name="capture_analysis_dispatch_failure",
        ),
        CheckConstraint(
            "failure_code IS NULL OR length(failure_code) > 0",
            name="capture_analysis_dispatch_failure_present",
        ),
        CheckConstraint(
            "failure_stage IS NULL OR length(failure_stage) > 0",
            name="capture_analysis_dispatch_failure_stage_present",
        ),
        CheckConstraint(
            "failure_detail_code IS NULL OR length(failure_detail_code) > 0",
            name="capture_analysis_dispatch_failure_detail_present",
        ),
        Index(
            "ix_capture_analysis_dispatch_due",
            "status",
            "scheduled_at",
            "analysis_run_id",
        ),
        Index(
            "ix_capture_analysis_dispatch_lease",
            "status",
            "dispatch_locked_until",
            "analysis_run_id",
        ),
    )

    analysis_run_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    capture_session_id: Mapped[UUID] = mapped_column(
        ForeignKey("capture_session.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    move_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("move_job.id", ondelete="RESTRICT"),
        nullable=False,
    )
    submitted_by_participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[CaptureAnalysisStatus] = mapped_column(
        Enum(CaptureAnalysisStatus, name="capture_analysis_status", native_enum=False),
        nullable=False,
        default=CaptureAnalysisStatus.PENDING,
    )
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dispatch_attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    dispatch_token: Mapped[UUID | None] = mapped_column(Uuid)
    dispatch_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_task_id: Mapped[str | None] = mapped_column(String(512))
    last_dispatch_error_code: Mapped[str | None] = mapped_column(String(64))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    retryable: Mapped[bool | None] = mapped_column(Boolean)
    failure_stage: Mapped[str | None] = mapped_column(String(32))
    provider_status: Mapped[int | None] = mapped_column(Integer)
    failure_detail_code: Mapped[str | None] = mapped_column(String(64))
    scope_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("scope_version.id", ondelete="RESTRICT"),
        unique=True,
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
