"""Versioned event envelope for Outbox and provider event buses."""

from datetime import datetime
from enum import StrEnum

from pydantic import Field, JsonValue, field_validator, model_validator

from app.contracts.model import ContractModel
from app.contracts.primitives import AggregateId, EventId, ParticipantId, TraceId, utc_now


class DomainEventType(StrEnum):
    """Initial integration events, versioned in the event name."""

    CAPTURE_SUBMITTED_V1 = "capture_submitted.v1"
    ANALYSIS_COMPLETED_V1 = "analysis_completed.v1"
    ANALYSIS_FAILED_V1 = "analysis_failed.v1"
    SCOPE_LOCKED_V1 = "scope_locked.v1"
    CHANGE_REQUESTED_V1 = "change_requested.v1"
    COMPLETION_MEDIA_SUBMITTED_V1 = "completion_media_submitted.v1"
    MEDIA_DELETED_V1 = "media_deleted.v1"


class DomainEvent(ContractModel):
    """Provider-neutral event with explicit schema and deduplication identity."""

    event_id: EventId
    event_type: DomainEventType
    schema_version: int = Field(default=1, ge=1)
    aggregate_id: AggregateId
    occurred_at: datetime = Field(default_factory=utc_now)
    actor_id: ParticipantId | None = None
    trace_id: TraceId
    payload: dict[str, JsonValue]

    @field_validator("occurred_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        """Reject local or ambiguous event timestamps."""

        if value.tzinfo is None or value.utcoffset() is None:
            msg = "occurred_at must include a timezone"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def require_matching_versions(self) -> "DomainEvent":
        """Keep the event-name suffix and envelope schema version aligned."""

        suffix = self.event_type.rsplit(".v", maxsplit=1)[-1]
        if int(suffix) != self.schema_version:
            msg = "schema_version must match the event_type version suffix"
            raise ValueError(msg)
        return self
