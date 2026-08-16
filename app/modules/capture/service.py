"""Capture session and storage-port application commands."""

import secrets
from datetime import timedelta
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.events import DomainEventType
from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.ports import ProviderError, ProviderErrorKind, StoragePort
from app.contracts.primitives import utc_now
from app.modules.analysis_workflow.models import CaptureAnalysisDispatch
from app.modules.analysis_workflow.schemas import capture_analysis_response
from app.modules.background_job.service import create_media_validation_background_job
from app.modules.capture.models import CaptureSession, MediaAsset
from app.modules.capture.schemas import (
    MEDIA_CONSENT_POLICY_VERSION,
    MEDIA_PROCESSING_PURPOSES,
    CaptureSessionCreate,
    CaptureSessionDetailResponse,
    CaptureSessionResponse,
    MediaAssetResponse,
    MediaProcessingConsentSnapshot,
    MediaUploadCreate,
    MediaUploadResponse,
)
from app.modules.completion.models import AuditEventType
from app.modules.completion.service import add_audit_event
from app.modules.move_job.models import (
    JobParticipant,
    Location,
    MoveJob,
    MoveJobStatus,
    RoomZone,
)
from app.platform.event_bus import enqueue_domain_event

UPLOAD_URL_TTL_SECONDS = 15 * 60
STORAGE_TIMEOUT_SECONDS = 5.0
CAPTURE_PURPOSES = frozenset(
    {
        MediaPurpose.INVENTORY,
        MediaPurpose.CONDITION,
        MediaPurpose.CHANGE_EVIDENCE,
        MediaPurpose.COMPLETION,
    }
)


class CaptureResourceNotFoundError(LookupError):
    """Raised for missing or cross-participant capture resources."""


class CaptureWorkflowConflictError(ValueError):
    """Raised when a terminal move job no longer accepts capture mutations."""


class MediaConsentConflictError(ValueError):
    """Raised when media work has no current explicit consent snapshot."""


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


def _consent_snapshot(capture: CaptureSession) -> MediaProcessingConsentSnapshot:
    return MediaProcessingConsentSnapshot(
        policy_version=capture.media_consent_policy_version,
        processing_purposes=(
            MEDIA_PROCESSING_PURPOSES if capture.privacy_notice_acknowledged else ()
        ),
        privacy_notice_acknowledged=capture.privacy_notice_acknowledged,
        retention_days_after_job_completion=capture.media_retention_days,
        consented_at=capture.media_consented_at,
    )


def _capture_response(capture: CaptureSession) -> CaptureSessionResponse:
    return CaptureSessionResponse(
        id=capture.id,
        job_id=capture.job_id,
        created_by_participant_id=capture.created_by_participant_id,
        media_processing_consent=_consent_snapshot(capture),
        created_at=capture.created_at,
    )


async def _lock_mutable_job(session: AsyncSession, job_id: UUID) -> None:
    job = await session.scalar(select(MoveJob).where(MoveJob.id == job_id).with_for_update())
    if job is None:
        raise CaptureResourceNotFoundError(job_id)
    if job.status in {MoveJobStatus.COMPLETED, MoveJobStatus.CANCELED}:
        raise CaptureWorkflowConflictError(job_id)


def _validated_upload_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        if value != value.strip() or parsed.scheme.lower() != "https" or parsed.hostname is None:
            raise ValueError
    except ValueError:
        raise ProviderError(
            ProviderErrorKind.UNAVAILABLE,
            "storage returned an invalid upload URL",
            retryable=False,
        ) from None
    return value


async def create_capture_session(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
    command: CaptureSessionCreate,
    *,
    retention_days: int,
) -> CaptureSessionResponse:
    if command.consent_policy_version != MEDIA_CONSENT_POLICY_VERSION:
        raise MediaConsentConflictError(command.consent_policy_version)
    await _lock_mutable_job(session, job_id)
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
        media_consent_policy_version=MEDIA_CONSENT_POLICY_VERSION,
        privacy_notice_acknowledged=True,
        media_retention_days=retention_days,
        media_consented_at=utc_now(),
    )
    session.add(capture_session)
    await session.flush()
    return _capture_response(capture_session)


async def list_capture_sessions(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
) -> tuple[CaptureSessionDetailResponse, ...]:
    """List only the caller's sessions with durable media and analysis state."""

    capture_sessions = (
        await session.scalars(
            select(CaptureSession)
            .where(
                CaptureSession.job_id == job_id,
                CaptureSession.created_by_participant_id == participant_id,
            )
            .order_by(CaptureSession.created_at.desc(), CaptureSession.id.desc())
        )
    ).all()
    if not capture_sessions:
        return ()

    capture_ids = tuple(capture.id for capture in capture_sessions)
    media_assets = (
        await session.scalars(
            select(MediaAsset)
            .where(MediaAsset.capture_session_id.in_(capture_ids))
            .order_by(MediaAsset.created_at, MediaAsset.id)
        )
    ).all()
    analyses = (
        await session.scalars(
            select(CaptureAnalysisDispatch).where(
                CaptureAnalysisDispatch.capture_session_id.in_(capture_ids)
            )
        )
    ).all()

    assets_by_capture: dict[UUID, list[MediaAssetResponse]] = {
        capture_id: [] for capture_id in capture_ids
    }
    for asset in media_assets:
        assets_by_capture[asset.capture_session_id].append(_asset_response(asset))
    analyses_by_capture = {row.capture_session_id: row for row in analyses}

    return tuple(
        CaptureSessionDetailResponse(
            id=capture.id,
            job_id=capture.job_id,
            created_by_participant_id=capture.created_by_participant_id,
            media_processing_consent=_consent_snapshot(capture),
            created_at=capture.created_at,
            media_assets=tuple(assets_by_capture[capture.id]),
            analysis=(
                capture_analysis_response(analyses_by_capture[capture.id])
                if capture.id in analyses_by_capture
                else None
            ),
        )
        for capture in capture_sessions
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


async def _lock_capture_for_media(
    session: AsyncSession,
    job_id: UUID,
    capture_session_id: UUID,
    participant_id: UUID,
) -> None:
    capture = await session.scalar(
        select(CaptureSession)
        .where(
            CaptureSession.id == capture_session_id,
            CaptureSession.job_id == job_id,
            CaptureSession.created_by_participant_id == participant_id,
        )
        .with_for_update()
    )
    if capture is None:
        raise CaptureResourceNotFoundError(capture_session_id)
    if (
        await session.scalar(
            select(CaptureAnalysisDispatch.analysis_run_id).where(
                CaptureAnalysisDispatch.capture_session_id == capture_session_id
            )
        )
        is not None
    ):
        raise CaptureWorkflowConflictError(capture_session_id)


async def create_media_upload(
    session: AsyncSession,
    storage: StoragePort,
    job_id: UUID,
    capture_session_id: UUID,
    participant_id: UUID,
    command: MediaUploadCreate,
) -> MediaUploadResponse:
    capture = await _load_owned_capture_session(
        session,
        job_id,
        capture_session_id,
        participant_id,
    )
    if capture.media_consented_at is None:
        raise MediaConsentConflictError(capture_session_id)
    job_status = await session.scalar(select(MoveJob.status).where(MoveJob.id == job_id))
    if job_status in {MoveJobStatus.COMPLETED, MoveJobStatus.CANCELED}:
        raise CaptureWorkflowConflictError(job_id)
    if command.media_purpose not in CAPTURE_PURPOSES:
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
    expires_at = utc_now() + timedelta(seconds=UPLOAD_URL_TTL_SECONDS)
    upload_target = await storage.create_upload_url(
        object_key=object_key,
        content_type=command.content_type,
        content_length=command.content_length,
        expires_in_seconds=UPLOAD_URL_TTL_SECONDS,
        timeout_seconds=STORAGE_TIMEOUT_SECONDS,
    )
    upload_url = _validated_upload_url(upload_target.url)

    await _lock_mutable_job(session, job_id)
    await _lock_capture_for_media(session, job_id, capture_session_id, participant_id)
    session.add(asset)
    await session.flush()
    return MediaUploadResponse(
        asset=_asset_response(asset),
        upload_url=upload_url,
        upload_headers=dict(upload_target.headers),
        expires_at=expires_at,
    )


async def complete_media_upload(
    session: AsyncSession,
    storage: StoragePort,
    job_id: UUID,
    capture_session_id: UUID,
    media_asset_id: UUID,
    participant_id: UUID,
    *,
    trace_id: str | None = None,
) -> MediaAssetResponse:
    capture = await _load_owned_capture_session(
        session,
        job_id,
        capture_session_id,
        participant_id,
    )
    if capture.media_consented_at is None:
        raise MediaConsentConflictError(capture_session_id)
    statement = select(MediaAsset).where(
        MediaAsset.id == media_asset_id,
        MediaAsset.capture_session_id == capture_session_id,
    )
    asset = (await session.scalars(statement)).one_or_none()
    if asset is None:
        raise CaptureResourceNotFoundError(media_asset_id)
    if asset.status is MediaAssetStatus.UPLOADED:
        await create_media_validation_background_job(
            session,
            job_id,
            asset.id,
            participant_id,
            trace_id=trace_id or secrets.token_hex(16),
        )
        return _asset_response(asset)
    if asset.status is not MediaAssetStatus.PENDING_UPLOAD:
        raise MediaUploadStateConflictError(media_asset_id)

    job_status = await session.scalar(select(MoveJob.status).where(MoveJob.id == job_id))
    if job_status in {MoveJobStatus.COMPLETED, MoveJobStatus.CANCELED}:
        raise CaptureWorkflowConflictError(job_id)

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
    ) or metadata.generation is None:
        raise MediaMetadataMismatchError(media_asset_id)

    await _lock_mutable_job(session, job_id)
    await _lock_capture_for_media(session, job_id, capture_session_id, participant_id)
    asset = (
        await session.scalars(
            select(MediaAsset)
            .where(
                MediaAsset.id == media_asset_id,
                MediaAsset.capture_session_id == capture_session_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).one()
    if asset.status is MediaAssetStatus.UPLOADED:
        await create_media_validation_background_job(
            session,
            job_id,
            asset.id,
            participant_id,
            trace_id=trace_id or secrets.token_hex(16),
        )
        return _asset_response(asset)
    if asset.status is not MediaAssetStatus.PENDING_UPLOAD:
        raise MediaUploadStateConflictError(media_asset_id)

    asset.status = MediaAssetStatus.UPLOADED
    asset.actual_size_bytes = metadata.size_bytes
    asset.sha256_hex = None
    asset.generation = metadata.generation
    asset.uploaded_at = utc_now()
    operation_trace_id = trace_id or secrets.token_hex(16)
    await create_media_validation_background_job(
        session,
        job_id,
        asset.id,
        participant_id,
        trace_id=operation_trace_id,
    )
    if asset.media_purpose is MediaPurpose.COMPLETION:
        add_audit_event(
            session,
            job_id,
            AuditEventType.COMPLETION_MEDIA_UPLOADED,
            actor_participant_id=participant_id,
            payload={
                "capture_session_id": str(capture_session_id),
                "media_asset_id": str(asset.id),
                "room_zone_id": str(asset.room_zone_id),
            },
        )
        enqueue_domain_event(
            session,
            DomainEventType.COMPLETION_MEDIA_SUBMITTED_V1,
            job_id,
            actor_id=participant_id,
            trace_id=operation_trace_id,
            payload={
                "capture_session_id": str(capture_session_id),
                "media_asset_id": str(asset.id),
                "room_zone_id": str(asset.room_zone_id),
            },
        )
    await session.flush()
    return _asset_response(asset)
