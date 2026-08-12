"""Capture and media upload HTTP schemas."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.model import ContractModel

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 200 * 1024 * 1024


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


class MediaUploadResponse(ContractModel):
    asset: MediaAssetResponse
    upload_url: Annotated[str, Field(min_length=1, repr=False)]
    upload_headers: Annotated[dict[str, str], Field(min_length=1, repr=False)]
    expires_at: datetime
