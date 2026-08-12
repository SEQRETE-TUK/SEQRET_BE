"""Authenticated media-retention job API."""

from datetime import timedelta
from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status

from app.contracts.actor import ParticipantRole
from app.contracts.primitives import utc_now
from app.modules.access.auth import CurrentActor, authorize_job_actor
from app.modules.background_job.schemas import BackgroundJobCreate, BackgroundJobResponse
from app.modules.background_job.service import (
    BackgroundJobConflictError,
    BackgroundJobNotFoundError,
    create_retention_background_job,
    list_background_jobs,
    retry_background_job,
)
from app.platform.db.dependencies import Session

router = APIRouter(prefix="/move-jobs", tags=["background-job"])
MAINTENANCE_ROLES = frozenset({ParticipantRole.COMPANY_MANAGER})


@router.post(
    "/{job_id}/background-jobs",
    response_model=BackgroundJobResponse,
    status_code=status.HTTP_201_CREATED,
    summary="보존기간 미디어 삭제 작업 생성",
)
async def create_background_job_endpoint(
    job_id: UUID,
    command: BackgroundJobCreate,
    request: Request,
    actor: CurrentActor,
    session: Session,
) -> BackgroundJobResponse:
    authorize_job_actor(actor, job_id, MAINTENANCE_ROLES)
    retention_days = request.app.state.runtime_context.settings.media_retention_days
    if retention_days is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="media retention policy is unavailable",
        )
    operation_time = utc_now()
    try:
        return await create_retention_background_job(
            session,
            job_id,
            command.media_asset_id,
            cast(UUID, actor.participant_id),
            retention_cutoff=operation_time - timedelta(days=retention_days),
            trace_id=actor.trace_id,
            scheduled_at=operation_time,
        )
    except BackgroundJobNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="media asset not found",
        ) from error
    except BackgroundJobConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="media asset is not eligible for retention deletion",
        ) from error


@router.get(
    "/{job_id}/background-jobs",
    response_model=tuple[BackgroundJobResponse, ...],
    summary="백그라운드 작업 조회",
)
async def list_background_jobs_endpoint(
    job_id: UUID,
    actor: CurrentActor,
    session: Session,
) -> tuple[BackgroundJobResponse, ...]:
    authorize_job_actor(actor, job_id)
    return await list_background_jobs(session, job_id)


@router.post(
    "/{job_id}/background-jobs/{background_job_id}/retry",
    response_model=BackgroundJobResponse,
    summary="실패한 백그라운드 작업 재실행",
)
async def retry_background_job_endpoint(
    job_id: UUID,
    background_job_id: UUID,
    actor: CurrentActor,
    session: Session,
) -> BackgroundJobResponse:
    authorize_job_actor(actor, job_id, MAINTENANCE_ROLES)
    try:
        return await retry_background_job(
            session,
            job_id,
            background_job_id,
        )
    except BackgroundJobNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="background job not found",
        ) from error
    except BackgroundJobConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="background job cannot be retried from its current state",
        ) from error
