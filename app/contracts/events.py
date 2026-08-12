"""Versioned event envelope for Outbox and provider event buses."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

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

    @model_validator(mode="after")
    def require_documented_payload(self) -> "DomainEvent":
        """Validate the exact payload emitted by current A-owned v1 events."""

        payload = self.payload
        valid = True
        try:
            if self.event_type is DomainEventType.SCOPE_LOCKED_V1:
                scope_version_id = payload.get("scope_version_id")
                content_hash = payload.get("content_hash")
                valid = (
                    set(payload) == {"scope_version_id", "content_hash"}
                    and isinstance(scope_version_id, str)
                    and str(UUID(scope_version_id)) == scope_version_id
                    and isinstance(content_hash, str)
                    and len(content_hash) == 64
                    and all(character in "0123456789abcdef" for character in content_hash)
                )
            elif self.event_type is DomainEventType.CHANGE_REQUESTED_V1:
                change_request_id = payload.get("change_request_id")
                base_scope_version_id = payload.get("base_scope_version_id")
                evidence_ids = payload.get("evidence_media_asset_ids")
                valid = (
                    set(payload)
                    == {
                        "change_request_id",
                        "base_scope_version_id",
                        "evidence_media_asset_ids",
                    }
                    and isinstance(change_request_id, str)
                    and str(UUID(change_request_id)) == change_request_id
                    and isinstance(base_scope_version_id, str)
                    and str(UUID(base_scope_version_id)) == base_scope_version_id
                    and isinstance(evidence_ids, list)
                    and bool(evidence_ids)
                    and all(
                        isinstance(media_asset_id, str)
                        and str(UUID(media_asset_id)) == media_asset_id
                        for media_asset_id in evidence_ids
                    )
                    and len(set(evidence_ids)) == len(evidence_ids)
                )
            elif self.event_type is DomainEventType.COMPLETION_MEDIA_SUBMITTED_V1:
                capture_session_id = payload.get("capture_session_id")
                media_asset_id = payload.get("media_asset_id")
                room_zone_id = payload.get("room_zone_id")
                valid = (
                    set(payload)
                    == {
                        "capture_session_id",
                        "media_asset_id",
                        "room_zone_id",
                    }
                    and isinstance(capture_session_id, str)
                    and str(UUID(capture_session_id)) == capture_session_id
                    and isinstance(media_asset_id, str)
                    and str(UUID(media_asset_id)) == media_asset_id
                    and isinstance(room_zone_id, str)
                    and str(UUID(room_zone_id)) == room_zone_id
                )
            elif self.event_type is DomainEventType.MEDIA_DELETED_V1:
                background_job_id = payload.get("background_job_id")
                media_asset_id = payload.get("media_asset_id")
                valid = (
                    set(payload) == {"background_job_id", "media_asset_id"}
                    and isinstance(background_job_id, str)
                    and str(UUID(background_job_id)) == background_job_id
                    and isinstance(media_asset_id, str)
                    and str(UUID(media_asset_id)) == media_asset_id
                )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(f"payload does not match {self.event_type} contract")
        return self
