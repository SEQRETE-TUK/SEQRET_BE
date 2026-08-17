"""A-owned quote proposals and customer revision requests."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
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

from app.platform.db.base import Base


class ScopeProposalKind(StrEnum):
    """Why a company produced a quoted scope version."""

    INITIAL = "initial"
    REVISION = "revision"


class ScopeProposalStatus(StrEnum):
    """Customer-facing state of one quoted immutable scope."""

    CUSTOMER_REVIEW = "customer_review"
    REVISION_REQUESTED = "revision_requested"
    CONFIRMED = "confirmed"
    SUPERSEDED = "superseded"


class ScopeProposal(Base):
    """One company-confirmed scope version with an immutable KRW quote snapshot."""

    __tablename__ = "scope_proposal"
    __table_args__ = (
        UniqueConstraint("source_scope_version_id"),
        UniqueConstraint("result_scope_version_id"),
        CheckConstraint(
            "kind IN ('INITIAL', 'REVISION')",
            name="scope_proposal_kind",
        ),
        CheckConstraint(
            "status IN ('CUSTOMER_REVIEW', 'REVISION_REQUESTED', 'CONFIRMED', 'SUPERSEDED')",
            name="scope_proposal_status",
        ),
        CheckConstraint(
            "base_amount_krw >= 0 AND total_amount_krw >= 0",
            name="scope_proposal_amount_nonnegative",
        ),
        CheckConstraint(
            "source_scope_version_id <> result_scope_version_id",
            name="scope_proposal_distinct_versions",
        ),
        CheckConstraint(
            "(status = 'CONFIRMED' AND confirmed_at IS NOT NULL) OR "
            "(status <> 'CONFIRMED' AND confirmed_at IS NULL)",
            name="scope_proposal_confirmation_time",
        ),
        Index(
            "ix_scope_proposal_job_status_sent",
            "job_id",
            "status",
            "sent_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("move_job.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_scope_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("scope_version.id", ondelete="RESTRICT"),
        nullable=False,
    )
    result_scope_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("scope_version.id", ondelete="RESTRICT"),
        nullable=False,
    )
    proposed_by_participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    kind: Mapped[ScopeProposalKind] = mapped_column(
        Enum(ScopeProposalKind, name="scope_proposal_kind", native_enum=False),
        nullable=False,
    )
    status: Mapped[ScopeProposalStatus] = mapped_column(
        Enum(ScopeProposalStatus, name="scope_proposal_status", native_enum=False),
        nullable=False,
        default=ScopeProposalStatus.CUSTOMER_REVIEW,
    )
    base_amount_krw: Mapped[int] = mapped_column(BigInteger, nullable=False)
    adjustments: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    total_amount_krw: Mapped[int] = mapped_column(BigInteger, nullable=False)
    execution_plan: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    included_works: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    exclusions: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScopeRevisionRequest(Base):
    """A customer's durable request to replace one quoted scope version."""

    __tablename__ = "scope_revision_request"
    __table_args__ = (
        UniqueConstraint("scope_proposal_id"),
        UniqueConstraint("scope_version_id"),
        UniqueConstraint("resolved_by_scope_proposal_id"),
        CheckConstraint(
            "(resolved_by_scope_proposal_id IS NULL AND resolved_at IS NULL) OR "
            "(resolved_by_scope_proposal_id IS NOT NULL AND resolved_at IS NOT NULL)",
            name="scope_revision_request_resolution",
        ),
        Index(
            "ix_scope_revision_request_job_requested",
            "job_id",
            "requested_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("move_job.id", ondelete="CASCADE"),
        nullable=False,
    )
    scope_proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("scope_proposal.id", ondelete="RESTRICT"),
        nullable=False,
    )
    scope_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("scope_version.id", ondelete="RESTRICT"),
        nullable=False,
    )
    requested_by_participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    resolved_by_scope_proposal_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("scope_proposal.id", ondelete="RESTRICT"),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
