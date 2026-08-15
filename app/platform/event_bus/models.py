"""Transactional Outbox persistence owned by track A."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    Index,
    Integer,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.contracts.events import DomainEventType
from app.platform.db.base import Base


class OutboxEvent(Base):
    """One immutable event envelope with mutable delivery bookkeeping."""

    __tablename__ = "outbox_event"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ("
            "'CAPTURE_SUBMITTED_V1', 'ANALYSIS_COMPLETED_V1', "
            "'ANALYSIS_FAILED_V1', 'SCOPE_LOCKED_V1', 'CHANGE_REQUESTED_V1', "
            "'DISPATCH_CONFIRMED_V1', 'COMPLETION_MEDIA_SUBMITTED_V1', "
            "'MEDIA_DELETED_V1')",
            name="outbox_event_type",
        ),
        CheckConstraint("schema_version = 1", name="outbox_schema_version_one"),
        CheckConstraint(
            "json_typeof(payload) = 'object'",
            name="outbox_payload_object",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "json_type(payload) = 'object'",
            name="outbox_payload_object",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint("attempt_count >= 0", name="outbox_attempt_count_nonnegative"),
        CheckConstraint("length(trace_id) = 32", name="outbox_trace_id_length"),
        CheckConstraint(
            "last_error_code IS NULL OR length(last_error_code) > 0",
            name="outbox_error_code_present",
        ),
        CheckConstraint(
            "(lock_token IS NULL) = (locked_until IS NULL)",
            name="outbox_lock_pair",
        ),
        CheckConstraint(
            "published_at IS NULL OR "
            "(lock_token IS NULL AND locked_until IS NULL AND last_error_code IS NULL)",
            name="outbox_published_final",
        ),
        Index(
            "ix_outbox_event_delivery",
            "published_at",
            "next_attempt_at",
            "locked_until",
            "occurred_at",
        ),
    )

    event_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    event_type: Mapped[DomainEventType] = mapped_column(
        Enum(DomainEventType, name="domain_event_type", native_enum=False),
        nullable=False,
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(Uuid)
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    lock_token: Mapped[UUID | None] = mapped_column(Uuid)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
