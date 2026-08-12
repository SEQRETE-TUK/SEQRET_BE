"""A-owned completion confirmation and append-only audit persistence."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
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
