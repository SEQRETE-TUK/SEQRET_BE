"""A-owned immutable work-scope snapshots."""

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
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.contracts.actor import ParticipantRole
from app.platform.db.base import Base


class ScopeVersion(Base):
    """Append-only snapshot in one linear work-scope history."""

    __tablename__ = "scope_version"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "sequence_number",
            name="uq_scope_version_job_id_sequence_number",
        ),
        UniqueConstraint("parent_version_id"),
        CheckConstraint("sequence_number > 0", name="sequence_positive"),
        CheckConstraint("length(content_hash) = 64", name="content_hash_length"),
        CheckConstraint(
            "(source_analysis_run_id IS NULL AND source_capture_session_id IS NULL "
            "AND analysis_source IS NULL AND created_by_participant_id IS NOT NULL) OR "
            "(source_analysis_run_id IS NOT NULL AND source_capture_session_id IS NOT NULL "
            "AND analysis_source IS NOT NULL AND created_by_participant_id IS NULL)",
            name="scope_version_origin",
        ),
        Index(
            "uq_scope_version_initial_job",
            "job_id",
            unique=True,
            postgresql_where=text("parent_version_id IS NULL"),
            sqlite_where=text("parent_version_id IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("move_job.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("scope_version.id", ondelete="CASCADE"),
    )
    sequence_number: Mapped[int] = mapped_column(nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_analysis_run_id: Mapped[UUID | None] = mapped_column(Uuid, unique=True)
    source_capture_session_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("capture_session.id", ondelete="RESTRICT"),
    )
    analysis_source: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    created_by_participant_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScopeApproval(Base):
    """Append-only confirmation by one business side for one version."""

    __tablename__ = "scope_approval"
    __table_args__ = (
        UniqueConstraint("scope_version_id", "role"),
        CheckConstraint(
            "role IN ('CUSTOMER', 'COMPANY_MANAGER')",
            name="scope_approval_role",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scope_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("scope_version.id", ondelete="CASCADE"),
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
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class ChangeRequestStatus(StrEnum):
    """Small workflow from field proposal to a scope-version decision."""

    PENDING = "pending"
    CLARIFICATION_REQUESTED = "clarification_requested"
    APPROVED = "approved"
    REJECTED = "rejected"


class ChangeRequest(Base):
    """Field change proposed against one confirmed scope version."""

    __tablename__ = "change_request"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'CLARIFICATION_REQUESTED', 'APPROVED', 'REJECTED')",
            name="change_request_status",
        ),
        CheckConstraint(
            "(status IN ('PENDING', 'CLARIFICATION_REQUESTED') "
            "AND decided_by_participant_id IS NULL AND decided_at IS NULL "
            "AND decision_note IS NULL AND result_scope_version_id IS NULL) OR "
            "(status = 'APPROVED' AND decided_by_participant_id IS NOT NULL "
            "AND decided_at IS NOT NULL AND result_scope_version_id IS NOT NULL) OR "
            "(status = 'REJECTED' AND decided_by_participant_id IS NOT NULL "
            "AND decided_at IS NOT NULL AND decision_note IS NOT NULL "
            "AND result_scope_version_id IS NULL)",
            name="change_request_decision",
        ),
        CheckConstraint(
            "(clarification_requested_by_participant_id IS NULL "
            "AND clarification_request IS NULL AND clarification_requested_at IS NULL "
            "AND explanation IS NULL AND explained_at IS NULL) OR "
            "(clarification_requested_by_participant_id IS NOT NULL "
            "AND clarification_request IS NOT NULL AND clarification_requested_at IS NOT NULL "
            "AND ((status = 'CLARIFICATION_REQUESTED' "
            "AND explanation IS NULL AND explained_at IS NULL) OR "
            "(status IN ('PENDING', 'APPROVED', 'REJECTED') "
            "AND explanation IS NOT NULL AND explained_at IS NOT NULL)))",
            name="change_request_clarification",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("move_job.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    base_scope_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("scope_version.id", ondelete="CASCADE"),
        nullable=False,
    )
    requested_by_participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    description: Mapped[str] = mapped_column(String(2000), nullable=False)
    proposed_content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[ChangeRequestStatus] = mapped_column(
        Enum(ChangeRequestStatus, name="change_request_status", native_enum=False),
        nullable=False,
        default=ChangeRequestStatus.PENDING,
    )
    clarification_requested_by_participant_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT")
    )
    clarification_request: Mapped[str | None] = mapped_column(String(2000))
    clarification_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    explanation: Mapped[str | None] = mapped_column(String(2000))
    explained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by_participant_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT")
    )
    decision_note: Mapped[str | None] = mapped_column(String(2000))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_scope_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("scope_version.id", ondelete="RESTRICT"),
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class ChangeRequestEvidence(Base):
    """Evidence media attached to one field change."""

    __tablename__ = "change_request_evidence"

    change_request_id: Mapped[UUID] = mapped_column(
        ForeignKey("change_request.id", ondelete="CASCADE"),
        primary_key=True,
    )
    media_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("media_asset.id", ondelete="RESTRICT"),
        primary_key=True,
    )
