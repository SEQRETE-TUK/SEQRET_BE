"""Application commands for move job topology."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.move_job.models import JobParticipant, Location, MoveJob, RoomZone
from app.modules.move_job.schemas import (
    LocationResponse,
    MoveJobCreate,
    MoveJobResponse,
    ParticipantCreate,
    ParticipantResponse,
    RoomZoneResponse,
)


class MoveJobNotFoundError(LookupError):
    """Raised when a move job does not exist."""


class ParticipantRoleConflictError(ValueError):
    """Raised when one job already has the requested role."""


async def _load_move_job(session: AsyncSession, job_id: UUID) -> MoveJob | None:
    statement = (
        select(MoveJob)
        .where(MoveJob.id == job_id)
        .options(
            selectinload(MoveJob.participants),
            selectinload(MoveJob.locations).selectinload(Location.room_zones),
        )
    )
    return (await session.scalars(statement)).one_or_none()


def _to_response(job: MoveJob) -> MoveJobResponse:
    return MoveJobResponse(
        id=job.id,
        title=job.title,
        status=job.status,
        scheduled_at=job.scheduled_at,
        created_at=job.created_at,
        participants=tuple(
            ParticipantResponse(
                id=participant.id,
                role=participant.role,
                display_name=participant.display_name,
            )
            for participant in sorted(job.participants, key=lambda item: item.role.value)
        ),
        locations=tuple(
            LocationResponse(
                id=location.id,
                kind=location.kind,
                label=location.label,
                room_zones=tuple(
                    RoomZoneResponse(
                        id=zone.id,
                        name=zone.name,
                        sort_order=zone.sort_order,
                    )
                    for zone in sorted(location.room_zones, key=lambda item: item.sort_order)
                ),
            )
            for location in sorted(job.locations, key=lambda item: item.kind.value)
        ),
    )


async def create_move_job(session: AsyncSession, command: MoveJobCreate) -> MoveJobResponse:
    """Create the job, participants, locations, and zones atomically."""

    job = MoveJob(title=command.title, scheduled_at=command.scheduled_at)
    job.participants = [
        JobParticipant(role=item.role, display_name=item.display_name)
        for item in command.participants
    ]
    job.locations = [
        Location(
            kind=item.kind,
            label=item.label,
            room_zones=[
                RoomZone(name=zone.name, sort_order=zone.sort_order) for zone in item.room_zones
            ],
        )
        for item in command.locations
    ]
    session.add(job)
    await session.flush()
    return _to_response(job)


async def get_move_job(session: AsyncSession, job_id: UUID) -> MoveJobResponse:
    """Return one complete topology without exposing ORM objects."""

    job = await _load_move_job(session, job_id)
    if job is None:
        raise MoveJobNotFoundError(job_id)
    return _to_response(job)


async def connect_participant(
    session: AsyncSession,
    job_id: UUID,
    command: ParticipantCreate,
) -> MoveJobResponse:
    """Connect one missing business role to an existing job."""

    job = await _load_move_job(session, job_id)
    if job is None:
        raise MoveJobNotFoundError(job_id)
    if any(participant.role is command.role for participant in job.participants):
        raise ParticipantRoleConflictError(command.role)

    job.participants.append(JobParticipant(role=command.role, display_name=command.display_name))
    try:
        await session.flush()
    except IntegrityError as error:
        raise ParticipantRoleConflictError(command.role) from error
    return _to_response(job)
