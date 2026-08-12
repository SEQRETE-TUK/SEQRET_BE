"""Authenticated immutable work-scope API."""

from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from app.contracts.actor import ParticipantRole
from app.modules.access.auth import CurrentActor, authorize_job_actor
from app.modules.scope.schemas import ScopeVersionCreate, ScopeVersionResponse
from app.modules.scope.service import (
    ScopeResourceNotFoundError,
    ScopeVersionConflictError,
    create_scope_version,
    list_scope_versions,
)
from app.platform.db.dependencies import Session

router = APIRouter(prefix="/move-jobs", tags=["scope"])


@router.post(
    "/{job_id}/scope-versions",
    response_model=ScopeVersionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="불변 작업범위 버전 생성",
)
async def create_scope_version_endpoint(
    job_id: UUID,
    command: ScopeVersionCreate,
    actor: CurrentActor,
    session: Session,
) -> ScopeVersionResponse:
    authorize_job_actor(
        actor,
        job_id,
        frozenset({ParticipantRole.CUSTOMER, ParticipantRole.COMPANY_MANAGER}),
    )
    try:
        return await create_scope_version(
            session,
            job_id,
            cast(UUID, actor.participant_id),
            command,
        )
    except ScopeResourceNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="scope resource not found",
        ) from error
    except ScopeVersionConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="scope parent is no longer current",
        ) from error


@router.get(
    "/{job_id}/scope-versions",
    response_model=tuple[ScopeVersionResponse, ...],
    summary="작업범위 버전 이력 조회",
)
async def list_scope_versions_endpoint(
    job_id: UUID,
    actor: CurrentActor,
    session: Session,
) -> tuple[ScopeVersionResponse, ...]:
    authorize_job_actor(actor, job_id)
    return await list_scope_versions(session, job_id)
