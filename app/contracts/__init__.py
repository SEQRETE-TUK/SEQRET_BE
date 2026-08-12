"""Versioned provider-independent contracts shared by both tracks."""

from app.contracts.actor import ActorContext, ActorKind, ParticipantRole
from app.contracts.ai import AnalysisRequest, AnalysisResult, DraftItem
from app.contracts.errors import ErrorDetail, ErrorResponse
from app.contracts.events import DomainEvent, DomainEventType
from app.contracts.media import MediaAssetRef, MediaAssetStatus, MediaPurpose
from app.contracts.ports import (
    AIProviderPort,
    EventBusPort,
    ObjectStoragePort,
    ProviderError,
    ProviderErrorKind,
    StorageObjectMetadata,
    StoragePort,
    TaskQueuePort,
)
from app.contracts.primitives import (
    AggregateId,
    AnalysisRunId,
    CaptureSessionId,
    EventId,
    IdempotencyKey,
    JobId,
    MediaAssetId,
    ParticipantId,
    RequestId,
    RoomZoneId,
    TraceId,
    utc_now,
)

__all__ = [
    "AIProviderPort",
    "ActorContext",
    "ActorKind",
    "AggregateId",
    "AnalysisRequest",
    "AnalysisResult",
    "AnalysisRunId",
    "CaptureSessionId",
    "DomainEvent",
    "DomainEventType",
    "DraftItem",
    "ErrorDetail",
    "ErrorResponse",
    "EventBusPort",
    "EventId",
    "IdempotencyKey",
    "JobId",
    "MediaAssetId",
    "MediaAssetRef",
    "MediaAssetStatus",
    "MediaPurpose",
    "ObjectStoragePort",
    "ParticipantId",
    "ParticipantRole",
    "ProviderError",
    "ProviderErrorKind",
    "RequestId",
    "RoomZoneId",
    "StorageObjectMetadata",
    "StoragePort",
    "TaskQueuePort",
    "TraceId",
    "utc_now",
]
