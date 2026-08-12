"""A-owned immutable work-scope snapshots."""

from datetime import datetime
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
