"""Deduplicated event consumption and participant notification persistence."""

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
from sqlalchemy.orm import Mapped, mapped_column

from app.contracts.events import DomainEventType
from app.platform.db.base import Base


class NotificationStatus(StrEnum):
    """Provider-neutral notification delivery state."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class EventConsumption(Base):
    """One durable idempotency receipt per event consumer."""

    __tablename__ = "event_consumption"
    __table_args__ = (CheckConstraint("length(consumer_name) > 0", name="consumer_name_present"),)

    consumer_name: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class NotificationDelivery(Base):
    """A recipient-specific notification fact without contact details or message text."""

    __tablename__ = "notification_delivery"
    __table_args__ = (
        UniqueConstraint("event_id", "recipient_participant_id"),
        CheckConstraint(
            "event_type IN ("
            "'CAPTURE_SUBMITTED_V1', 'ANALYSIS_COMPLETED_V1', "
            "'ANALYSIS_FAILED_V1', 'SCOPE_LOCKED_V1', 'CHANGE_REQUESTED_V1', "
            "'DISPATCH_CONFIRMED_V1', 'COMPLETION_MEDIA_SUBMITTED_V1', "
            "'COMPLETION_SUBMITTED_V1', 'COMPLETION_REQUESTED_V1', "
            "'COMPLETION_DECIDED_V1', "
            "'MEDIA_DELETED_V1')",
            name="notification_event_type",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'SENT', 'FAILED')",
            name="notification_status",
        ),
        CheckConstraint("attempt_count >= 0", name="notification_attempt_count_nonnegative"),
        CheckConstraint(
            "(status = 'SENT') = (sent_at IS NOT NULL)",
            name="notification_sent_time",
        ),
        CheckConstraint(
            "(status = 'FAILED') = (last_error_code IS NOT NULL)",
            name="notification_failure_code",
        ),
        CheckConstraint(
            "last_error_code IS NULL OR length(last_error_code) > 0",
            name="notification_error_code_present",
        ),
        CheckConstraint(
            "(attempt_count > 0) = (last_attempt_at IS NOT NULL)",
            name="notification_attempt_time",
        ),
        Index(
            "ix_notification_delivery_recipient_created",
            "recipient_participant_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    event_type: Mapped[DomainEventType] = mapped_column(
        Enum(DomainEventType, name="domain_event_type", native_enum=False),
        nullable=False,
    )
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("move_job.id", ondelete="RESTRICT"),
        nullable=False,
    )
    recipient_participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_participant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus, name="notification_status", native_enum=False),
        nullable=False,
        default=NotificationStatus.PENDING,
    )
    attempt_count: Mapped[int] = mapped_column(
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
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
