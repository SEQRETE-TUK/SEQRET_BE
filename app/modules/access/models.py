"""Hashed participant access credentials."""

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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.contracts.actor import ParticipantRole
from app.modules.move_job.models import JobParticipant
from app.platform.db.base import Base


class ParticipantAccessToken(Base):
    """Revocable credential whose plaintext is never persisted."""

    __tablename__ = "participant_access_token"
    __table_args__ = (
        CheckConstraint("length(token_hash) = 64", name="token_hash_length"),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        CheckConstraint(
            "(rate_window_started_at IS NULL AND rate_window_count = 0) OR "
            "(rate_window_started_at IS NOT NULL AND rate_window_count > 0)",
            name="rate_window_state",
        ),
        Index(
            "uq_participant_access_token_active_participant",
            "participant_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_participant.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rate_window_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rate_window_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    participant: Mapped[JobParticipant] = relationship(lazy="raise")


class InvitationStatus(StrEnum):
    """Lifecycle of a capability offered to a new job participant."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"
    REVOKED = "revoked"


class ParticipantInvitation(Base):
    """One role invitation whose plaintext capability is never persisted."""

    __tablename__ = "participant_invitation"
    __table_args__ = (
        UniqueConstraint("job_id", "role"),
        UniqueConstraint("access_link_id"),
        CheckConstraint(
            "role IN ('COMPANY_MANAGER', 'FIELD_WORKER')",
            name="participant_invitation_role",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'ACCEPTED', 'DECLINED', 'EXPIRED', 'REVOKED')",
            name="participant_invitation_status",
        ),
        CheckConstraint(
            "(status IN ('PENDING', 'EXPIRED') AND resolved_at IS NULL) OR "
            "(status IN ('ACCEPTED', 'DECLINED', 'REVOKED') AND resolved_at IS NOT NULL)",
            name="participant_invitation_resolution",
        ),
        CheckConstraint(
            "expires_at > issued_at",
            name="participant_invitation_expiry_after_issue",
        ),
        CheckConstraint(
            "issuer_participant_id <> invitee_participant_id",
            name="participant_invitation_distinct_participants",
        ),
        Index("ix_participant_invitation_job_status", "job_id", "status", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("move_job.id", ondelete="CASCADE"),
        nullable=False,
    )
    issuer_participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    invitee_participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    access_link_id: Mapped[UUID] = mapped_column(
        ForeignKey("participant_access_token.id", ondelete="RESTRICT"),
        nullable=False,
    )
    role: Mapped[ParticipantRole] = mapped_column(
        Enum(ParticipantRole, name="participant_role", native_enum=False),
        nullable=False,
    )
    status: Mapped[InvitationStatus] = mapped_column(
        Enum(
            InvitationStatus,
            name="participant_invitation_status",
            native_enum=False,
        ),
        nullable=False,
        default=InvitationStatus.PENDING,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    issuer: Mapped[JobParticipant] = relationship(
        foreign_keys=[issuer_participant_id],
        lazy="raise",
    )
    invitee: Mapped[JobParticipant] = relationship(
        foreign_keys=[invitee_participant_id],
        lazy="raise",
    )
    access_link: Mapped[ParticipantAccessToken] = relationship(lazy="raise")
