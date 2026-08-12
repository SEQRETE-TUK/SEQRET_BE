"""A-owned immutable work-scope snapshots."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

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
    created_by_participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
