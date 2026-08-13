"""Durable background-job persistence for media retention."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
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

from app.contracts.maintenance import BackgroundJobType
from app.platform.db.base import Base


class BackgroundJobStatus(StrEnum):
    """Small state machine separating durable intent from external dispatch."""

    PENDING = "pending"
    DISPATCHING = "dispatching"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class BackgroundJob(Base):
    """One immutable media target with retryable dispatch bookkeeping."""

    __tablename__ = "background_job"
    __table_args__ = (
        UniqueConstraint(
            "job_type",
            "media_asset_id",
            name="uq_background_job_type_media_asset_id",
        ),
        CheckConstraint(
            "job_type IN ('MEDIA_VALIDATION', 'MEDIA_RETENTION_DELETE')",
            name="background_job_type",
        ),
        CheckConstraint(
            "(job_type = 'MEDIA_VALIDATION' AND target_content_type IS NOT NULL "
            "AND length(target_content_type) > 0 AND target_size_bytes > 0) OR "
            "(job_type = 'MEDIA_RETENTION_DELETE' AND target_content_type IS NULL "
            "AND target_size_bytes IS NULL)",
            name="background_job_target_shape",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'DISPATCHING', 'QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')",
            name="background_job_status",
        ),
        CheckConstraint("attempt_count >= 0", name="background_job_attempt_nonnegative"),
        CheckConstraint("length(target_object_key) > 0", name="background_job_target_present"),
        CheckConstraint(
            "length(target_generation) > 0",
            name="background_job_generation_present",
        ),
        CheckConstraint("length(trace_id) = 32", name="background_job_trace_id_length"),
        CheckConstraint(
            "last_error_code IS NULL OR length(last_error_code) > 0",
            name="background_job_error_code_present",
        ),
        CheckConstraint(
            "(attempt_count = 0) = (last_attempt_at IS NULL)",
            name="background_job_attempt_time",
        ),
        CheckConstraint(
            "(status = 'DISPATCHING' AND dispatch_token IS NOT NULL "
            "AND dispatch_locked_until IS NOT NULL) OR "
            "(status <> 'DISPATCHING' AND dispatch_token IS NULL "
            "AND dispatch_locked_until IS NULL)",
            name="background_job_dispatch_lease",
        ),
        CheckConstraint(
            "(status = 'FAILED') = (last_error_code IS NOT NULL)",
            name="background_job_failure_code",
        ),
        CheckConstraint(
            "(status IN ('SUCCEEDED', 'FAILED')) = (completed_at IS NOT NULL)",
            name="background_job_completion_time",
        ),
        CheckConstraint(
            "(status = 'RUNNING') = (execution_deadline_at IS NOT NULL)",
            name="background_job_execution_deadline",
        ),
        Index(
            "ix_background_job_dispatch",
            "status",
            "scheduled_at",
            "id",
        ),
        Index(
            "ix_background_job_dispatch_lease",
            "status",
            "dispatch_locked_until",
            "id",
        ),
        Index(
            "ix_background_job_move_job_created",
            "move_job_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    move_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("move_job.id", ondelete="RESTRICT"),
        nullable=False,
    )
    media_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("media_asset.id", ondelete="RESTRICT"),
        nullable=False,
    )
    job_type: Mapped[BackgroundJobType] = mapped_column(
        Enum(BackgroundJobType, name="background_job_type", native_enum=False),
        nullable=False,
    )
    status: Mapped[BackgroundJobStatus] = mapped_column(
        Enum(BackgroundJobStatus, name="background_job_status", native_enum=False),
        nullable=False,
        default=BackgroundJobStatus.PENDING,
    )
    target_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    target_generation: Mapped[str] = mapped_column(String(255), nullable=False)
    target_content_type: Mapped[str | None] = mapped_column(String(255))
    target_size_bytes: Mapped[int | None] = mapped_column(Integer)
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    dispatch_token: Mapped[UUID | None] = mapped_column(Uuid)
    dispatch_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_task_id: Mapped[str | None] = mapped_column(String(512))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_by_participant_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
