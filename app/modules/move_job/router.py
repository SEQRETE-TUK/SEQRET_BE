"""Move job HTTP API."""

from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials

from app.api.errors import protected_error_responses
from app.contracts.actor import ParticipantRole
from app.modules.access.auth import (
    CurrentActor,
    WorkspaceCookieSecret,
    _authenticate_request,
    authorize_job_actor,
    bearer,
)
from app.modules.access.workspace import (
    InvalidWorkspaceSessionError,
    authenticate_workspace_account,
)
from app.modules.move_job.models import MoveJobStatus
from app.modules.move_job.schemas import (
    CustomerMoveJobCreate,
    CustomerMoveJobCreatedResponse,
    MoveJobCreate,
    MoveJobCreatedResponse,
    MoveJobListResponse,
    MoveJobPatch,
    MoveJobResponse,
)
from app.modules.move_job.service import (
    MoveJobConflictError,
    MoveJobNotFoundError,
    cancel_move_job,
    create_customer_move_job,
    create_move_job,
    get_move_job,
    list_move_jobs,
    patch_move_job,
)
from app.platform.db.dependencies import Session

router = APIRouter(prefix="/move-jobs", tags=["move-jobs"])


@router.post(
    "/onboarding",
    response_model=CustomerMoveJobCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="소비자 작업과 전용 링크 생성",
)
async def create_customer_move_job_endpoint(
    command: CustomerMoveJobCreate,
    response: Response,
    session: Session,
) -> CustomerMoveJobCreatedResponse:
    created = await create_customer_move_job(session, command)
    response.headers["Cache-Control"] = "no-store"
    return created


@router.post(
    "",
    response_model=MoveJobCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="작업과 초기 공간 구성 생성",
)
async def create_move_job_endpoint(
    command: MoveJobCreate,
    response: Response,
    session: Session,
) -> MoveJobCreatedResponse:
    created = await create_move_job(session, command)
    response.headers["Cache-Control"] = "no-store"
    return created


@router.get(
    "",
    response_model=MoveJobListResponse,
    responses=protected_error_responses(),
    summary="내 작업공간의 이사 목록 검색·필터",
)
async def list_move_jobs_endpoint(
    request: Request,
    response: Response,
    session: Session,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    cookie_secret: WorkspaceCookieSecret,
    status_filter: Annotated[MoveJobStatus | None, Query(alias="status")] = None,
    search: Annotated[str | None, Query(alias="q", min_length=1, max_length=100)] = None,
    scheduled_from: datetime | None = None,
    scheduled_to: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> MoveJobListResponse:
    account_id: UUID | None = None
    participant_id: UUID | None = None
    if credentials is not None:
        actor = await _authenticate_request(
            credentials.credentials,
            request,
            allow_pending_invitation=False,
        )
        participant_id = cast(UUID, actor.participant_id)
    else:
        if cookie_secret is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid access token",
            )
        try:
            principal = await authenticate_workspace_account(session, cookie_secret)
        except InvalidWorkspaceSessionError as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid workspace session",
            ) from error
        account_id = principal.account_id
    try:
        result = await list_move_jobs(
            session,
            actor_participant_id=participant_id,
            account_id=account_id,
            status_filter=status_filter,
            search=search,
            scheduled_from=scheduled_from,
            scheduled_to=scheduled_to,
            limit=limit,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get(
    "/{job_id}",
    response_model=MoveJobResponse,
    responses=protected_error_responses(status.HTTP_404_NOT_FOUND),
    summary="작업 구성 조회",
)
async def get_move_job_endpoint(
    job_id: UUID,
    actor: CurrentActor,
    session: Session,
) -> MoveJobResponse:
    authorize_job_actor(actor, job_id)
    try:
        return await get_move_job(session, job_id)
    except MoveJobNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="move job not found"
        ) from error


@router.patch(
    "/{job_id}",
    response_model=MoveJobResponse,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
    summary="견적 전 이사 기본정보 수정",
)
async def patch_move_job_endpoint(
    job_id: UUID,
    command: MoveJobPatch,
    actor: CurrentActor,
    session: Session,
) -> MoveJobResponse:
    authorize_job_actor(actor, job_id, frozenset({ParticipantRole.CUSTOMER}))
    try:
        return await patch_move_job(
            session,
            job_id,
            cast(UUID, actor.participant_id),
            command,
        )
    except MoveJobNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="move job not found",
        ) from error
    except MoveJobConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="confirmed-scope, quoted, completed, or canceled move job cannot be edited",
        ) from error


@router.delete(
    "/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
    summary="견적 전 소비자 작업 취소",
)
async def cancel_move_job_endpoint(
    job_id: UUID,
    actor: CurrentActor,
    session: Session,
) -> Response:
    authorize_job_actor(actor, job_id, frozenset({ParticipantRole.CUSTOMER}))
    try:
        await cancel_move_job(
            session,
            job_id,
            cast(UUID, actor.participant_id),
        )
    except MoveJobNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="move job not found",
        ) from error
    except MoveJobConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="quoted or completed move job cannot be canceled",
        ) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)
