"""Move job HTTP API."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status

from app.api.errors import protected_error_responses
from app.modules.access.auth import CurrentActor, authorize_job_actor
from app.modules.move_job.schemas import (
    MoveJobCreate,
    MoveJobCreatedResponse,
    MoveJobResponse,
)
from app.modules.move_job.service import (
    MoveJobNotFoundError,
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
    response: Response,
    session: Session,
) -> MoveJobCreatedResponse:
    created = await create_move_job(session, command)
    response.headers["Cache-Control"] = "no-store"
    return created


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
