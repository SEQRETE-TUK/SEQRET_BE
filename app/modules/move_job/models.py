"""A-owned move job, participant, location, and room zone models."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.contracts.actor import ParticipantRole
from app.platform.db.base import Base


class MoveJobStatus(StrEnum):
    """Initial lifecycle before capture and scope workflows extend it."""

    DRAFT = "draft"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELED = "canceled"


class LocationKind(StrEnum):
    """Physical endpoint of a move job."""

    ORIGIN = "origin"
    DESTINATION = "destination"


class MoveJob(Base):
    """Root aggregate for one move agreement."""

    __tablename__ = "move_job"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT', 'ACTIVE', 'COMPLETED', 'CANCELED')",
            name="move_job_status",
        ),
        CheckConstraint(
            "(status = 'COMPLETED') = (completed_at IS NOT NULL)",
            name="move_job_completion",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[MoveJobStatus] = mapped_column(
        Enum(
            MoveJobStatus,
            name="move_job_status",
            native_enum=False,
        ),
        nullable=False,
        default=MoveJobStatus.DRAFT,
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    participants: Mapped[list["JobParticipant"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="raise",
    )
    locations: Mapped[list["Location"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="raise",
    )


class JobParticipant(Base):
    """Role membership scoped to exactly one move job."""

    __tablename__ = "job_participant"
    __table_args__ = (
        UniqueConstraint("job_id", "role"),
        CheckConstraint(
            "role IN ('CUSTOMER', 'COMPANY_MANAGER', 'FIELD_WORKER')",
            name="participant_role",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("move_job.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[ParticipantRole] = mapped_column(
        Enum(
            ParticipantRole,
            name="participant_role",
            native_enum=False,
        ),
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    job: Mapped[MoveJob] = relationship(back_populates="participants", lazy="raise")


class Location(Base):
    """Origin or destination with no raw postal address persisted."""

    __tablename__ = "location"
    __table_args__ = (
        UniqueConstraint("job_id", "kind"),
        CheckConstraint(
            "kind IN ('ORIGIN', 'DESTINATION')",
            name="location_kind",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("move_job.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[LocationKind] = mapped_column(
        Enum(
            LocationKind,
            name="location_kind",
            native_enum=False,
        ),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    job: Mapped[MoveJob] = relationship(back_populates="locations", lazy="raise")
    room_zones: Mapped[list["RoomZone"]] = relationship(
        back_populates="location",
        cascade="all, delete-orphan",
        lazy="raise",
    )


class RoomZone(Base):
    """Ordered capture and scope subdivision inside one location."""

    __tablename__ = "room_zone"
    __table_args__ = (
        UniqueConstraint("location_id", "name"),
        UniqueConstraint(
            "location_id",
            "sort_order",
            name="uq_room_zone_location_id_sort_order",
        ),
        CheckConstraint("sort_order >= 0", name="ck_room_zone_sort_order_nonnegative"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    location_id: Mapped[UUID] = mapped_column(
        ForeignKey("location.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    sort_order: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    location: Mapped[Location] = relationship(back_populates="room_zones", lazy="raise")
