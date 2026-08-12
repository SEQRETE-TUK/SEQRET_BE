"""Move job HTTP API and transaction dependency."""

from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.move_job.schemas import MoveJobCreate, MoveJobResponse, ParticipantCreate
from app.modules.move_job.service import (
    MoveJobNotFoundError,
    ParticipantRoleConflictError,
    connect_participant,
    create_move_job,
    get_move_job,
)
from app.platform.db import transactional_session

router = APIRouter(prefix="/move-jobs", tags=["move-jobs"])


async def get_move_job_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.database_session_factory
    async with transactional_session(factory) as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_move_job_session)]


@router.post(
    "",
    response_model=MoveJobResponse,
    status_code=status.HTTP_201_CREATED,
    summary="작업과 초기 공간 구성 생성",
)
async def create_move_job_endpoint(command: MoveJobCreate, session: Session) -> MoveJobResponse:
    return await create_move_job(session, command)


@router.get(
    "/{job_id}",
    response_model=MoveJobResponse,
    summary="작업 구성 조회",
)
async def get_move_job_endpoint(job_id: UUID, session: Session) -> MoveJobResponse:
    try:
        return await get_move_job(session, job_id)
    except MoveJobNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="move job not found"
        ) from error


@router.post(
    "/{job_id}/participants",
    response_model=MoveJobResponse,
    status_code=status.HTTP_201_CREATED,
    summary="작업 참여자 연결",
)
async def connect_participant_endpoint(
    job_id: UUID,
    command: ParticipantCreate,
    session: Session,
) -> MoveJobResponse:
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
