"""Capture session and storage-port application commands."""

from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.ports import StoragePort
from app.contracts.primitives import utc_now
from app.modules.capture.models import CaptureSession, MediaAsset
from app.modules.capture.schemas import (
    CaptureSessionResponse,
    MediaAssetResponse,
    MediaUploadCreate,
    MediaUploadResponse,
)
from app.modules.move_job.models import JobParticipant, Location, RoomZone

UPLOAD_URL_TTL_SECONDS = 15 * 60
STORAGE_TIMEOUT_SECONDS = 5.0
INITIAL_CAPTURE_PURPOSES = frozenset({MediaPurpose.INVENTORY, MediaPurpose.CONDITION})


class CaptureResourceNotFoundError(LookupError):
    """Raised for missing or cross-participant capture resources."""


class MediaPurposeNotAllowedError(ValueError):
    """Raised when a later workflow purpose is used in initial capture."""


class MediaMetadataMismatchError(ValueError):
    """Raised when the uploaded object violates the signed request."""


class MediaUploadStateConflictError(ValueError):
    """Raised when an upload cannot complete from the current state."""


def _asset_response(asset: MediaAsset) -> MediaAssetResponse:
    return MediaAssetResponse(
        id=asset.id,
        capture_session_id=asset.capture_session_id,
        room_zone_id=asset.room_zone_id,
        media_purpose=asset.media_purpose,
        status=asset.status,
        content_type=asset.content_type,
        expected_size_bytes=asset.expected_size_bytes,
        actual_size_bytes=asset.actual_size_bytes,
        sha256_hex=asset.sha256_hex,
        created_at=asset.created_at,
        uploaded_at=asset.uploaded_at,
    )


async def create_capture_session(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
) -> CaptureSessionResponse:
    participant = await session.scalar(
        select(JobParticipant.id).where(
            JobParticipant.id == participant_id,
            JobParticipant.job_id == job_id,
        )
    )
    if participant is None:
        raise CaptureResourceNotFoundError(job_id)
    capture_session = CaptureSession(
        job_id=job_id,
        created_by_participant_id=participant_id,
    )
    session.add(capture_session)
    await session.flush()
    return CaptureSessionResponse(
        id=capture_session.id,
        job_id=capture_session.job_id,
        created_by_participant_id=capture_session.created_by_participant_id,
        created_at=capture_session.created_at,
    )


async def _load_owned_capture_session(
    session: AsyncSession,
    job_id: UUID,
    capture_session_id: UUID,
    participant_id: UUID,
) -> CaptureSession:
    statement = select(CaptureSession).where(
        CaptureSession.id == capture_session_id,
        CaptureSession.job_id == job_id,
        CaptureSession.created_by_participant_id == participant_id,
    )
    capture_session = (await session.scalars(statement)).one_or_none()
    if capture_session is None:
        raise CaptureResourceNotFoundError(capture_session_id)
    return capture_session


async def create_media_upload(
    session: AsyncSession,
    storage: StoragePort,
    job_id: UUID,
    capture_session_id: UUID,
    participant_id: UUID,
    command: MediaUploadCreate,
) -> MediaUploadResponse:
    await _load_owned_capture_session(session, job_id, capture_session_id, participant_id)
    if command.media_purpose not in INITIAL_CAPTURE_PURPOSES:
        raise MediaPurposeNotAllowedError(command.media_purpose)
    room_zone = await session.scalar(
        select(RoomZone.id)
        .join(RoomZone.location)
        .where(RoomZone.id == command.room_zone_id, Location.job_id == job_id)
    )
    if room_zone is None:
        raise CaptureResourceNotFoundError(command.room_zone_id)

    asset_id = uuid4()
    object_key = f"jobs/{job_id}/captures/{capture_session_id}/{asset_id}"
    asset = MediaAsset(
        id=asset_id,
        capture_session_id=capture_session_id,
        room_zone_id=command.room_zone_id,
        media_purpose=command.media_purpose,
        object_key=object_key,
        content_type=command.content_type,
        expected_size_bytes=command.content_length,
    )
    session.add(asset)
    await session.flush()
    expires_at = utc_now() + timedelta(seconds=UPLOAD_URL_TTL_SECONDS)
    upload_url = await storage.create_upload_url(
        object_key=object_key,
        content_type=command.content_type,
        content_length=command.content_length,
        expires_in_seconds=UPLOAD_URL_TTL_SECONDS,
        timeout_seconds=STORAGE_TIMEOUT_SECONDS,
    )
    return MediaUploadResponse(
        asset=_asset_response(asset),
        upload_url=upload_url,
        expires_at=expires_at,
    )


async def complete_media_upload(
    session: AsyncSession,
    storage: StoragePort,
    job_id: UUID,
    capture_session_id: UUID,
    media_asset_id: UUID,
    participant_id: UUID,
) -> MediaAssetResponse:
    await _load_owned_capture_session(session, job_id, capture_session_id, participant_id)
    statement = select(MediaAsset).where(
        MediaAsset.id == media_asset_id,
        MediaAsset.capture_session_id == capture_session_id,
    )
    asset = (await session.scalars(statement)).one_or_none()
    if asset is None:
        raise CaptureResourceNotFoundError(media_asset_id)
    if asset.status is MediaAssetStatus.UPLOADED:
        return _asset_response(asset)
    if asset.status is not MediaAssetStatus.PENDING_UPLOAD:
        raise MediaUploadStateConflictError(media_asset_id)

    metadata = await storage.get_metadata(
        object_key=asset.object_key,
        timeout_seconds=STORAGE_TIMEOUT_SECONDS,
    )
    if (
        metadata.object_key,
        metadata.content_type,
        metadata.size_bytes,
    ) != (
        asset.object_key,
        asset.content_type,
        asset.expected_size_bytes,
    ):
        raise MediaMetadataMismatchError(media_asset_id)
    asset.status = MediaAssetStatus.UPLOADED
    asset.actual_size_bytes = metadata.size_bytes
    asset.sha256_hex = metadata.sha256_hex
    asset.generation = metadata.generation
    asset.uploaded_at = utc_now()
    await session.flush()
    return _asset_response(asset)
