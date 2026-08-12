"""Authenticated immutable work-scope API."""

from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from app.contracts.actor import ParticipantRole
from app.modules.access.auth import CurrentActor, authorize_job_actor
from app.modules.scope.schemas import (
    ChangeClarificationCreate,
    ChangeDecisionCreate,
    ChangeExplanationCreate,
    ChangeRequestCreate,
    ChangeRequestResponse,
    ScopeApprovalResult,
    ScopeVersionCreate,
    ScopeVersionResponse,
)
from app.modules.scope.service import (
    ChangeRequestConflictError,
    ChangeRequestInvalidError,
    ScopeApprovalConflictError,
    ScopeResourceNotFoundError,
    ScopeVersionConflictError,
    approve_scope_version,
    create_change_request,
    create_scope_version,
    decide_change_request,
    explain_change_request,
    list_change_requests,
    list_scope_versions,
    request_change_clarification,
)
from app.platform.db.dependencies import Session

router = APIRouter(prefix="/move-jobs", tags=["scope"])

APPROVER_ROLES = frozenset({ParticipantRole.CUSTOMER, ParticipantRole.COMPANY_MANAGER})


def _change_error(error: Exception) -> HTTPException:
    if isinstance(error, ScopeResourceNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="change resource not found",
        )
    if isinstance(error, ChangeRequestInvalidError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="change request is invalid",
        )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="change request state conflict",
    )


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
        APPROVER_ROLES,
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


@router.post(
    "/{job_id}/scope-versions/{scope_version_id}/approvals",
    response_model=ScopeApprovalResult,
    status_code=status.HTTP_201_CREATED,
    summary="작업범위 버전 확인",
)
async def approve_scope_version_endpoint(
    job_id: UUID,
    scope_version_id: UUID,
    actor: CurrentActor,
    session: Session,
) -> ScopeApprovalResult:
    authorize_job_actor(actor, job_id, APPROVER_ROLES)
    try:
        return await approve_scope_version(
            session,
            job_id,
            scope_version_id,
            cast(UUID, actor.participant_id),
            cast(ParticipantRole, actor.participant_role),
        )
    except ScopeResourceNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="scope resource not found",
        ) from error
    except ScopeApprovalConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="scope version cannot be approved",
        ) from error


@router.post(
    "/{job_id}/change-requests",
    response_model=ChangeRequestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="현장 변경요청 생성",
)
async def create_change_request_endpoint(
    job_id: UUID,
    command: ChangeRequestCreate,
    actor: CurrentActor,
    session: Session,
) -> ChangeRequestResponse:
    authorize_job_actor(actor, job_id, frozenset({ParticipantRole.FIELD_WORKER}))
    try:
        return await create_change_request(
            session,
            job_id,
            cast(UUID, actor.participant_id),
            command,
        )
    except (
        ScopeResourceNotFoundError,
        ChangeRequestConflictError,
        ChangeRequestInvalidError,
    ) as error:
        raise _change_error(error) from error


@router.get(
    "/{job_id}/change-requests",
    response_model=tuple[ChangeRequestResponse, ...],
    summary="현장 변경요청 목록 조회",
)
async def list_change_requests_endpoint(
    job_id: UUID,
    actor: CurrentActor,
    session: Session,
) -> tuple[ChangeRequestResponse, ...]:
    authorize_job_actor(actor, job_id)
    return await list_change_requests(session, job_id)


@router.post(
    "/{job_id}/change-requests/{change_request_id}/clarification",
    response_model=ChangeRequestResponse,
    summary="현장 변경 설명 요청",
)
async def request_change_clarification_endpoint(
    job_id: UUID,
    change_request_id: UUID,
    command: ChangeClarificationCreate,
    actor: CurrentActor,
    session: Session,
) -> ChangeRequestResponse:
    authorize_job_actor(actor, job_id, APPROVER_ROLES)
    try:
        return await request_change_clarification(
            session,
            job_id,
            change_request_id,
            cast(UUID, actor.participant_id),
            command.message,
        )
    except (ScopeResourceNotFoundError, ChangeRequestConflictError) as error:
        raise _change_error(error) from error


@router.post(
    "/{job_id}/change-requests/{change_request_id}/explanation",
    response_model=ChangeRequestResponse,
    summary="현장 변경 설명 제출",
)
async def explain_change_request_endpoint(
    job_id: UUID,
    change_request_id: UUID,
    command: ChangeExplanationCreate,
    actor: CurrentActor,
    session: Session,
) -> ChangeRequestResponse:
    authorize_job_actor(actor, job_id, frozenset({ParticipantRole.FIELD_WORKER}))
    try:
        return await explain_change_request(
            session,
            job_id,
            change_request_id,
            cast(UUID, actor.participant_id),
            command.explanation,
        )
    except (ScopeResourceNotFoundError, ChangeRequestConflictError) as error:
        raise _change_error(error) from error


@router.post(
    "/{job_id}/change-requests/{change_request_id}/decision",
    response_model=ChangeRequestResponse,
    summary="현장 변경 승인 또는 거절",
)
async def decide_change_request_endpoint(
    job_id: UUID,
    change_request_id: UUID,
    command: ChangeDecisionCreate,
    actor: CurrentActor,
    session: Session,
) -> ChangeRequestResponse:
    authorize_job_actor(actor, job_id, APPROVER_ROLES)
    try:
        return await decide_change_request(
            session,
            job_id,
            change_request_id,
            cast(UUID, actor.participant_id),
            command,
        )
    except (ScopeResourceNotFoundError, ChangeRequestConflictError) as error:
        raise _change_error(error) from error
