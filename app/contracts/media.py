"""Media metadata contracts shared without exposing ORM models."""

from enum import StrEnum

from app.contracts.model import ContractModel
from app.contracts.primitives import JobId, MediaAssetId, RoomZoneId


class MediaPurpose(StrEnum):
    """Allowed evidence purpose selected by track A policy."""

    INVENTORY = "inventory"
    CONDITION = "condition"
    CHANGE_EVIDENCE = "change_evidence"
    COMPLETION = "completion"


class MediaAssetStatus(StrEnum):
    """Provider-neutral lifecycle visible across track boundaries."""

    PENDING_UPLOAD = "pending_upload"
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


class MediaAssetRef(ContractModel):
    """A-owned media identity safe for B processing inputs."""

    media_asset_id: MediaAssetId
    job_id: JobId
    room_zone_id: RoomZoneId
    media_purpose: MediaPurpose
    status: MediaAssetStatus
