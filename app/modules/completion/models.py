"""A-owned completion confirmation and append-only audit persistence."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.contracts.actor import ParticipantRole
from app.platform.db.base import Base


class AuditEventType(StrEnum):
    """Major business facts visible in the job audit timeline."""

    JOB_CREATED = "job_created"
    PARTICIPANT_CONNECTED = "participant_connected"
    ACCESS_LINK_ISSUED = "access_link_issued"
    ACCESS_LINK_REVOKED = "access_link_revoked"
    COMPLETION_MEDIA_UPLOADED = "completion_media_uploaded"
    SCOPE_VERSION_CREATED = "scope_version_created"
    SCOPE_VERSION_APPROVED = "scope_version_approved"
    SCOPE_VERSION_LOCKED = "scope_version_locked"
    CHANGE_REQUESTED = "change_requested"
    CHANGE_CLARIFICATION_REQUESTED = "change_clarification_requested"
    CHANGE_EXPLAINED = "change_explained"
    CHANGE_APPROVED = "change_approved"
    CHANGE_REJECTED = "change_rejected"
    COMPLETION_CONFIRMED = "completion_confirmed"
    JOB_COMPLETED = "job_completed"


class CompletionRequestStatus(StrEnum):
    """Persisted lifecycle of one customer completion-review request."""

    REQUESTED = "requested"
    CONFIRMED = "confirmed"
    ISSUE_REPORTED = "issue_reported"
    REVOKED = "revoked"


class CompletionProblemType(StrEnum):
    """Customer-selected classification without automated responsibility judgment."""

    MISSING_WORK = "missing_work"
    DAMAGE = "damage"
    AMOUNT = "amount"
    OTHER = "other"


class CompletionSubmission(Base):
    """One immutable representative-worker record of field completion."""

    __tablename__ = "completion_submission"
    __table_args__ = (
        UniqueConstraint("job_id", "client_reference"),
        CheckConstraint("length(command_hash) = 64", name="command_hash_length"),
        CheckConstraint(
            "onsite_customer_confirmed = true",
            name="onsite_customer_confirmed",
        ),
        Index("ix_completion_submission_job_submitted", "job_id", "submitted_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("move_job.id", ondelete="CASCADE"),
        nullable=False,
    )
    client_reference: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    dispatch_plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("dispatch_plan.id", ondelete="RESTRICT"),
        nullable=False,
    )
    scope_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("scope_version.id", ondelete="RESTRICT"),
        nullable=False,
    )
    submitted_by_participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    command_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    completed_check_keys: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    worker_shifts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    onsite_customer_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    onsite_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    work_ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class CompletionSubmissionEvidence(Base):
    """Generation-pinned completion media selected by one immutable submission."""

    __tablename__ = "completion_submission_evidence"

    completion_submission_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_submission.id", ondelete="CASCADE"),
        primary_key=True,
    )
    media_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("media_asset.id", ondelete="RESTRICT"),
        primary_key=True,
    )


class CompletionRequest(Base):
    """One expiring company request for the customer to review a submission."""

    __tablename__ = "completion_request"
    __table_args__ = (
        UniqueConstraint("job_id", "client_reference"),
        CheckConstraint("length(command_hash) = 64", name="command_hash_length"),
        CheckConstraint(
            "status IN ('REQUESTED', 'CONFIRMED', 'ISSUE_REPORTED', 'REVOKED')",
            name="completion_request_status",
        ),
        CheckConstraint("expires_at > requested_at", name="completion_request_expiry_order"),
        CheckConstraint(
            "(status = 'REVOKED' AND revoked_at IS NOT NULL AND revoke_reason IS NOT NULL) OR "
            "(status <> 'REVOKED' AND revoked_at IS NULL AND revoke_reason IS NULL)",
            name="completion_request_revocation",
        ),
        CheckConstraint(
            "(status IN ('CONFIRMED', 'ISSUE_REPORTED') "
            "AND decided_by_participant_id IS NOT NULL AND decision_hash IS NOT NULL "
            "AND decided_at IS NOT NULL) OR "
            "(status NOT IN ('CONFIRMED', 'ISSUE_REPORTED') "
            "AND decided_by_participant_id IS NULL AND decision_hash IS NULL "
            "AND decided_at IS NULL)",
            name="completion_request_decision",
        ),
        Index("ix_completion_request_job_requested", "job_id", "requested_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("move_job.id", ondelete="CASCADE"),
        nullable=False,
    )
    client_reference: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    completion_submission_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_submission.id", ondelete="RESTRICT"),
        nullable=False,
    )
    requested_by_participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    command_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[CompletionRequestStatus] = mapped_column(
        Enum(
            CompletionRequestStatus,
            name="completion_request_status",
            native_enum=False,
        ),
        nullable=False,
        default=CompletionRequestStatus.REQUESTED,
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(String(2000))
    decided_by_participant_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT")
    )
    decision_hash: Mapped[str | None] = mapped_column(String(64))
    unrecorded_extra_charge: Mapped[bool | None] = mapped_column(Boolean)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CompletionProblemReport(Base):
    """One immutable customer report that does not assign cause or liability."""

    __tablename__ = "completion_problem_report"
    __table_args__ = (
        UniqueConstraint("completion_request_id"),
        CheckConstraint(
            "problem_type IN ('MISSING_WORK', 'DAMAGE', 'AMOUNT', 'OTHER')",
            name="completion_problem_type",
        ),
        Index("ix_completion_problem_job_reported", "job_id", "reported_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("move_job.id", ondelete="RESTRICT"),
        nullable=False,
    )
    completion_request_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_request.id", ondelete="RESTRICT"),
        nullable=False,
    )
    problem_type: Mapped[CompletionProblemType] = mapped_column(
        Enum(
            CompletionProblemType,
            name="completion_problem_type",
            native_enum=False,
        ),
        nullable=False,
    )
    description: Mapped[str] = mapped_column(String(2000), nullable=False)
    reported_by_participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class CompletionConfirmation(Base):
    """One side's immutable confirmation of the completed move."""

    __tablename__ = "completion_confirmation"
    __table_args__ = (
        UniqueConstraint("job_id", "role"),
        CheckConstraint(
            "role IN ('CUSTOMER', 'COMPANY_MANAGER')",
            name="completion_confirmation_role",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("move_job.id", ondelete="CASCADE"),
        nullable=False,
    )
    scope_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("scope_version.id", ondelete="RESTRICT"),
        nullable=False,
    )
    participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    role: Mapped[ParticipantRole] = mapped_column(
        Enum(ParticipantRole, name="participant_role", native_enum=False),
        nullable=False,
    )
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class CompletionEvidence(Base):
    """Completion media tied to the immutable bilateral confirmation."""

    __tablename__ = "completion_evidence"

    confirmation_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_confirmation.id", ondelete="CASCADE"),
        primary_key=True,
    )
    media_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("media_asset.id", ondelete="RESTRICT"),
        primary_key=True,
    )


class AuditEvent(Base):
    """Append-only, job-scoped business event without secrets or free-form text."""

    __tablename__ = "audit_event"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ("
            "'JOB_CREATED', 'PARTICIPANT_CONNECTED', 'ACCESS_LINK_ISSUED', "
            "'ACCESS_LINK_REVOKED', 'COMPLETION_MEDIA_UPLOADED', "
            "'SCOPE_VERSION_CREATED', 'SCOPE_VERSION_APPROVED', "
            "'SCOPE_VERSION_LOCKED', 'CHANGE_REQUESTED', "
            "'CHANGE_CLARIFICATION_REQUESTED', 'CHANGE_EXPLAINED', "
            "'CHANGE_APPROVED', 'CHANGE_REJECTED', 'COMPLETION_CONFIRMED', "
            "'JOB_COMPLETED')",
            name="audit_event_type",
        ),
        Index("ix_audit_event_job_occurred", "job_id", "occurred_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("move_job.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_type: Mapped[AuditEventType] = mapped_column(
        Enum(AuditEventType, name="audit_event_type", native_enum=False),
        nullable=False,
    )
    actor_participant_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
