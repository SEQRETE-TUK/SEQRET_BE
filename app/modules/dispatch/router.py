"""Frontend and integration routes for dispatch and field arrival."""

from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status

from app.api.errors import protected_error_responses
from app.contracts.actor import ParticipantRole
from app.modules.access.auth import CurrentActor, authorize_job_actor
from app.modules.dispatch.schemas import (
    DispatchConfirmCreate,
    DispatchSetupCreate,
    DispatchView,
    FieldBriefView,
    FieldCheckInCreate,
    FieldCheckInResponse,
)
from app.modules.dispatch.service import (
    DispatchConflictError,
    DispatchInvalidError,
    DispatchNotFoundError,
    check_in_field_worker,
    confirm_dispatch,
    create_dispatch_setup,
    get_dispatch_view,
    get_field_brief,
)
from app.platform.db.dependencies import Session

router = APIRouter(prefix="/move-jobs", tags=["dispatch"])


def _not_found(error: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="dispatch resource not found")


def _conflict(error: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail="dispatch state conflict")


@router.post(
    "/{job_id}/dispatch/setup",
    response_model=DispatchView,
    status_code=status.HTTP_201_CREATED,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
    summary="업체 배차 요구사항과 후보 snapshot 등록",
)
async def create_dispatch_setup_endpoint(
    job_id: UUID,
    command: DispatchSetupCreate,
    response: Response,
    actor: CurrentActor,
    session: Session,
) -> DispatchView:
    authorize_job_actor(actor, job_id, frozenset({ParticipantRole.COMPANY_MANAGER}))
    participant_id = cast(UUID, actor.participant_id)
    try:
        await create_dispatch_setup(session, job_id, participant_id, command)
        view = await get_dispatch_view(session, job_id, participant_id)
    except DispatchNotFoundError as error:
        raise _not_found(error) from error
    except (DispatchConflictError, DispatchInvalidError) as error:
        raise _conflict(error) from error
    response.headers["Cache-Control"] = "no-store"
    return view


@router.get(
    "/{job_id}/dispatch",
    response_model=DispatchView,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
    summary="업체 배차 후보와 검증 상태 조회",
)
async def get_dispatch_endpoint(
    job_id: UUID,
    response: Response,
    actor: CurrentActor,
    session: Session,
) -> DispatchView:
    authorize_job_actor(actor, job_id, frozenset({ParticipantRole.COMPANY_MANAGER}))
    try:
        view = await get_dispatch_view(session, job_id, cast(UUID, actor.participant_id))
    except DispatchNotFoundError as error:
        raise _not_found(error) from error
    except DispatchConflictError as error:
        raise _conflict(error) from error
    response.headers["Cache-Control"] = "no-store"
    return view


@router.put(
    "/{job_id}/dispatch",
    response_model=DispatchView,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
    summary="업체 배차 확정과 현장기사 알림 생성",
)
async def confirm_dispatch_endpoint(
    job_id: UUID,
    command: DispatchConfirmCreate,
    response: Response,
    actor: CurrentActor,
    session: Session,
) -> DispatchView:
    authorize_job_actor(actor, job_id, frozenset({ParticipantRole.COMPANY_MANAGER}))
    participant_id = cast(UUID, actor.participant_id)
    try:
        await confirm_dispatch(
            session,
            job_id,
            participant_id,
            command,
            trace_id=actor.trace_id,
        )
        view = await get_dispatch_view(session, job_id, participant_id)
    except DispatchNotFoundError as error:
        raise _not_found(error) from error
    except DispatchConflictError as error:
        raise _conflict(error) from error
    response.headers["Cache-Control"] = "no-store"
    return view


@router.get(
    "/{job_id}/field-brief",
    response_model=FieldBriefView,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
    summary="배정 현장기사 작업 브리프 조회",
)
async def get_field_brief_endpoint(
    job_id: UUID,
    response: Response,
    actor: CurrentActor,
    session: Session,
) -> FieldBriefView:
    authorize_job_actor(actor, job_id, frozenset({ParticipantRole.FIELD_WORKER}))
    try:
        view = await get_field_brief(session, job_id, cast(UUID, actor.participant_id))
    except DispatchNotFoundError as error:
        raise _not_found(error) from error
    except DispatchConflictError as error:
        raise _conflict(error) from error
    response.headers["Cache-Control"] = "no-store"
    return view


@router.post(
    "/{job_id}/check-ins",
    response_model=FieldCheckInResponse,
    status_code=status.HTTP_201_CREATED,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
    summary="배정 현장기사 안전 확인과 도착 체크인",
)
async def check_in_field_worker_endpoint(
    job_id: UUID,
    command: FieldCheckInCreate,
    actor: CurrentActor,
    session: Session,
) -> FieldCheckInResponse:
    authorize_job_actor(actor, job_id, frozenset({ParticipantRole.FIELD_WORKER}))
    try:
        return await check_in_field_worker(
            session,
            job_id,
            cast(UUID, actor.participant_id),
            command,
        )
    except DispatchNotFoundError as error:
        raise _not_found(error) from error
    except DispatchConflictError as error:
        raise _conflict(error) from error
