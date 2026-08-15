"""A-owned field issue and customer-facing change proposal persistence."""

from datetime import datetime
from enum import StrEnum
from typing import Any
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


class FieldIssueType(StrEnum):
    """The operator-visible reason a worker stopped or questioned field work."""

    OUT_OF_SCOPE = "out_of_scope"
    DAMAGE_RISK = "damage_risk"
    SITE_BLOCKER = "site_blocker"


class FieldIssue(Base):
    """Immutable worker report against the exact current confirmed scope."""

    __tablename__ = "field_issue"
    __table_args__ = (
        UniqueConstraint("job_id", "client_reference"),
        CheckConstraint(
            "issue_type IN ('OUT_OF_SCOPE', 'DAMAGE_RISK', 'SITE_BLOCKER')",
            name="field_issue_type",
        ),
        Index("ix_field_issue_job_created", "job_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("move_job.id", ondelete="CASCADE"),
        nullable=False,
    )
    client_reference: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    base_scope_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("scope_version.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reported_by_participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    issue_type: Mapped[FieldIssueType] = mapped_column(
        Enum(FieldIssueType, name="field_issue_type", native_enum=False),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class FieldIssueEvidence(Base):
    """Validated change-evidence media attached to one field report."""

    __tablename__ = "field_issue_evidence"

    field_issue_id: Mapped[UUID] = mapped_column(
        ForeignKey("field_issue.id", ondelete="CASCADE"),
        primary_key=True,
    )
    media_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("media_asset.id", ondelete="RESTRICT"),
        primary_key=True,
    )


class ChangeProposalDetail(Base):
    """Immutable quote metadata layered on the existing A-07 change request."""

    __tablename__ = "change_proposal_detail"
    __table_args__ = (
        UniqueConstraint("field_issue_id"),
        CheckConstraint(
            "base_amount_krw >= 0 AND total_amount_krw >= 0",
            name="change_proposal_amount_nonnegative",
        ),
    )

    change_request_id: Mapped[UUID] = mapped_column(
        ForeignKey("change_request.id", ondelete="CASCADE"),
        primary_key=True,
    )
    field_issue_id: Mapped[UUID] = mapped_column(
        ForeignKey("field_issue.id", ondelete="RESTRICT"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    base_amount_krw: Mapped[int] = mapped_column(BigInteger, nullable=False)
    adjustments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    total_amount_krw: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
