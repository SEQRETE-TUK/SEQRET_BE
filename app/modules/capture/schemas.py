"""Capture and media upload HTTP schemas."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.model import ContractModel
from app.modules.analysis_workflow.schemas import CaptureAnalysisResponse

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 200 * 1024 * 1024
MEDIA_CONSENT_POLICY_VERSION = "2026-08-17.v1"


class MediaProcessingPurpose(StrEnum):
    INVENTORY_ANALYSIS = "inventory_analysis"
    CONDITION_RECORD = "condition_record"
    FIELD_CHANGE_EVIDENCE = "field_change_evidence"
    COMPLETION_RECORD = "completion_record"


MEDIA_PROCESSING_PURPOSES = tuple(MediaProcessingPurpose)


class CaptureSessionCreate(ContractModel):
    model_config = ConfigDict(strict=False)

    consent_policy_version: Annotated[str, Field(min_length=1, max_length=50)]
    privacy_notice_acknowledged: bool

    @model_validator(mode="after")
    def require_explicit_acknowledgement(self) -> "CaptureSessionCreate":
        if self.privacy_notice_acknowledged is not True:
            raise ValueError("privacy notice acknowledgement is required")
        return self


class MediaConsentPolicyResponse(ContractModel):
    policy_version: str
    processing_purposes: tuple[MediaProcessingPurpose, ...]
    retention_days_after_job_completion: int
    notice: str


class MediaProcessingConsentSnapshot(ContractModel):
    policy_version: str | None
    processing_purposes: tuple[MediaProcessingPurpose, ...]
    privacy_notice_acknowledged: bool
    retention_days_after_job_completion: int | None
    consented_at: datetime | None


class MediaUploadCreate(ContractModel):
    model_config = ConfigDict(strict=False)

    room_zone_id: UUID
    media_purpose: MediaPurpose
    content_type: Literal["image/jpeg", "image/png", "video/mp4"]
    content_length: Annotated[int, Field(gt=0, le=MAX_VIDEO_BYTES)]

    @model_validator(mode="after")
    def enforce_content_limit(self) -> "MediaUploadCreate":
        if self.content_type.startswith("image/") and self.content_length > MAX_IMAGE_BYTES:
            raise ValueError("image exceeds the 20 MiB limit")
        return self


class CaptureSessionResponse(ContractModel):
    id: UUID
    job_id: UUID
    created_by_participant_id: UUID
    media_processing_consent: MediaProcessingConsentSnapshot
    created_at: datetime


class MediaAssetResponse(ContractModel):
    id: UUID
    capture_session_id: UUID
    room_zone_id: UUID
    media_purpose: MediaPurpose
    status: MediaAssetStatus
    content_type: str
    expected_size_bytes: int
    actual_size_bytes: int | None
    sha256_hex: str | None
    created_at: datetime
    uploaded_at: datetime | None


class CaptureSessionDetailResponse(CaptureSessionResponse):
    """Recoverable participant-owned capture state without storage capabilities."""

    media_assets: tuple[MediaAssetResponse, ...]
    analysis: CaptureAnalysisResponse | None


class MediaUploadResponse(ContractModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    asset: MediaAssetResponse
    upload_url: Annotated[
        str,
        Field(min_length=1, repr=False, json_schema_extra={"format": "uri"}),
    ]
    upload_headers: Annotated[dict[str, str], Field(min_length=1, repr=False)]
    expires_at: datetime
