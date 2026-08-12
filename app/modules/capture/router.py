"""Authenticated capture session and media upload API."""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.contracts.ports import ProviderError, ProviderErrorKind, StoragePort
from app.modules.access.auth import CurrentActor, authorize_job_actor
from app.modules.capture.schemas import (
    CaptureSessionResponse,
    MediaAssetResponse,
    MediaUploadCreate,
    MediaUploadResponse,
)
from app.modules.capture.service import (
    CaptureResourceNotFoundError,
    MediaMetadataMismatchError,
    MediaPurposeNotAllowedError,
    MediaUploadStateConflictError,
    complete_media_upload,
    create_capture_session,
    create_media_upload,
)
from app.platform.db.dependencies import Session

router = APIRouter(prefix="/move-jobs", tags=["capture"])


def get_storage_port(request: Request, job_id: UUID, actor: CurrentActor) -> StoragePort:
    authorize_job_actor(actor, job_id)
    storage = getattr(request.app.state, "storage_port", None)
    if storage is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="storage is unavailable",
        )
    return cast(StoragePort, storage)


Storage = Annotated[StoragePort, Depends(get_storage_port)]


def _storage_error(error: ProviderError) -> HTTPException:
    if error.kind in {
        ProviderErrorKind.NOT_FOUND,
        ProviderErrorKind.CONFLICT,
        ProviderErrorKind.INVALID_INPUT,
    }:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="media object is not ready",
        )
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="storage is unavailable",
    )


@router.post(
    "/{job_id}/capture-sessions",
    response_model=CaptureSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="촬영 세션 생성",
)
async def create_capture_session_endpoint(
    job_id: UUID,
    actor: CurrentActor,
    session: Session,
) -> CaptureSessionResponse:
    authorize_job_actor(actor, job_id)
    try:
        return await create_capture_session(session, job_id, cast(UUID, actor.participant_id))
    except CaptureResourceNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="move job not found",
        ) from error


@router.post(
    "/{job_id}/capture-sessions/{capture_session_id}/media-assets/upload",
    response_model=MediaUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="미디어 업로드 URL 발급",
)
async def create_media_upload_endpoint(
    job_id: UUID,
    capture_session_id: UUID,
    command: MediaUploadCreate,
    actor: CurrentActor,
    session: Session,
    storage: Storage,
) -> MediaUploadResponse:
    try:
        return await create_media_upload(
            session,
            storage,
            job_id,
            capture_session_id,
            cast(UUID, actor.participant_id),
            command,
        )
    except CaptureResourceNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="capture resource not found",
        ) from error
    except MediaPurposeNotAllowedError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="media purpose is not allowed for this capture workflow",
        ) from error
    except ProviderError as error:
        raise _storage_error(error) from error


@router.post(
    "/{job_id}/capture-sessions/{capture_session_id}/media-assets/{media_asset_id}/complete",
    response_model=MediaAssetResponse,
    summary="미디어 업로드 완료",
)
async def complete_media_upload_endpoint(
    job_id: UUID,
    capture_session_id: UUID,
    media_asset_id: UUID,
    actor: CurrentActor,
    session: Session,
    storage: Storage,
) -> MediaAssetResponse:
    try:
        return await complete_media_upload(
            session,
            storage,
            job_id,
            capture_session_id,
            media_asset_id,
            cast(UUID, actor.participant_id),
            trace_id=actor.trace_id,
        )
    except CaptureResourceNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="capture resource not found",
        ) from error
    except MediaMetadataMismatchError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="uploaded media metadata does not match",
        ) from error
    except MediaUploadStateConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="media upload cannot complete from its current state",
        ) from error
    except ProviderError as error:
        raise _storage_error(error) from error
