"""Move job HTTP API."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from app.contracts.actor import ParticipantRole
from app.modules.access.auth import CurrentActor, authorize_job_actor
from app.modules.move_job.schemas import (
    MoveJobCreate,
    MoveJobCreatedResponse,
    MoveJobResponse,
    ParticipantConnectedResponse,
    ParticipantCreate,
)
from app.modules.move_job.service import (
    MoveJobNotFoundError,
    ParticipantRoleConflictError,
    connect_participant,
    create_move_job,
    get_move_job,
)
from app.platform.db.dependencies import Session

router = APIRouter(prefix="/move-jobs", tags=["move-jobs"])


@router.post(
    "",
    response_model=MoveJobCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="작업과 초기 공간 구성 생성",
)
async def create_move_job_endpoint(
    command: MoveJobCreate,
    session: Session,
) -> MoveJobCreatedResponse:
    return await create_move_job(session, command)


@router.get(
    "/{job_id}",
    response_model=MoveJobResponse,
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


@router.post(
    "/{job_id}/participants",
    response_model=ParticipantConnectedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="작업 참여자 연결",
)
async def connect_participant_endpoint(
    job_id: UUID,
    command: ParticipantCreate,
    actor: CurrentActor,
    session: Session,
) -> ParticipantConnectedResponse:
    authorize_job_actor(
        actor,
        job_id,
        frozenset({ParticipantRole.CUSTOMER, ParticipantRole.COMPANY_MANAGER}),
    )
    try:
        return await connect_participant(session, job_id, command)
    except MoveJobNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="move job not found"
        ) from error
    except ParticipantRoleConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="participant role already exists",
        ) from error
