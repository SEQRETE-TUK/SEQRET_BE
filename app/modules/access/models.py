"""Hashed participant access credentials."""

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


class WorkspaceAccount(Base):
    """Durable same-role workspace created after a capability is proven once."""

    __tablename__ = "workspace_account"
    __table_args__ = (
        CheckConstraint(
            "role IN ('CUSTOMER', 'COMPANY_MANAGER', 'FIELD_WORKER')",
            name="workspace_account_role",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    role: Mapped[ParticipantRole] = mapped_column(
        Enum(ParticipantRole, name="participant_role", native_enum=False),
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class WorkspaceMembership(Base):
    """One participant attached to one durable role workspace."""

    __tablename__ = "workspace_membership"
    __table_args__ = (
        Index(
            "uq_workspace_membership_active_participant",
            "participant_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
        Index("ix_workspace_membership_account_joined", "account_id", "joined_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspace_account.id", ondelete="CASCADE"),
        nullable=False,
    )
    participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_participant.id", ondelete="CASCADE"),
        nullable=False,
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkspaceSession(Base):
    """Hashed browser session; the cookie plaintext is never persisted."""

    __tablename__ = "workspace_session"
    __table_args__ = (
        CheckConstraint("length(token_hash) = 64", name="workspace_session_token_hash_length"),
        CheckConstraint("length(csrf_token) >= 40", name="workspace_session_csrf_token_length"),
        CheckConstraint("expires_at > created_at", name="workspace_session_expiry_after_creation"),
        Index("ix_workspace_session_account_expiry", "account_id", "expires_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspace_account.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    csrf_token: Mapped[str] = mapped_column(String(100), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class NotificationContactChannel(StrEnum):
    """Externally deliverable channels owned by a workspace account."""

    EMAIL = "email"
    SMS = "sms"
    KAKAO = "kakao"


class WorkspaceContactPoint(Base):
    """Consent-backed external destination; API schemas keep it out of repr and logs."""

    __tablename__ = "workspace_contact_point"
    __table_args__ = (
        UniqueConstraint("account_id", "channel"),
        CheckConstraint(
            "channel IN ('EMAIL', 'SMS', 'KAKAO')",
            name="workspace_contact_point_channel",
        ),
        CheckConstraint("length(destination) > 0", name="workspace_contact_point_destination"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspace_account.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel: Mapped[NotificationContactChannel] = mapped_column(
        Enum(
            NotificationContactChannel,
            name="notification_contact_channel",
            native_enum=False,
        ),
        nullable=False,
    )
    destination: Mapped[str] = mapped_column(String(320), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
