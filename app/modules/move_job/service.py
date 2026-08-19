"""Application commands for move job topology."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.contracts.actor import ParticipantRole
from app.contracts.primitives import utc_now
from app.modules.access.models import (
    InvitationStatus,
    ParticipantAccessToken,
    ParticipantInvitation,
)
from app.modules.access.service import issue_access_link, revoke_access_link
from app.modules.completion.models import AuditEventType
from app.modules.completion.service import add_audit_event
from app.modules.move_job.models import (
    JobParticipant,
    Location,
    MoveJob,
    MoveJobStatus,
    RoomZone,
)
from app.modules.move_job.schemas import (
    CustomerMoveJobCreate,
    CustomerMoveJobCreatedResponse,
    LocationConditions,
    LocationCreate,
    LocationResponse,
    MoveJobCreate,
    MoveJobCreatedResponse,
    MoveJobResponse,
    ParticipantResponse,
    RoomZoneResponse,
)
from app.modules.scope_review.models import ScopeProposal


class MoveJobNotFoundError(LookupError):
    """Raised when a move job does not exist."""


class MoveJobConflictError(RuntimeError):
    """Raised when a move job can no longer be canceled."""


def _locations_from_command(locations: tuple[LocationCreate, ...]) -> list[Location]:
    """Build owned location rows from a validated creation command."""

    return [
        Location(
            kind=item.kind,
            label=item.label,
            conditions=item.conditions.model_dump(mode="json"),
            room_zones=[
                RoomZone(name=zone.name, sort_order=zone.sort_order) for zone in item.room_zones
            ],
        )
        for item in locations
    ]


def _record_job_created(session: AsyncSession, job: MoveJob) -> None:
    add_audit_event(
        session,
        job.id,
        AuditEventType.JOB_CREATED,
        payload={
            "participant_roles": sorted(participant.role.value for participant in job.participants),
            "location_kinds": sorted(location.kind.value for location in job.locations),
        },
    )


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
        completed_at=job.completed_at,
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
                conditions=LocationConditions.model_validate(location.conditions, strict=False),
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


async def create_move_job(session: AsyncSession, command: MoveJobCreate) -> MoveJobCreatedResponse:
    """Create the job, participants, locations, and zones atomically."""

    job = MoveJob(title=command.title, scheduled_at=command.scheduled_at)
    job.participants = [
        JobParticipant(role=item.role, display_name=item.display_name)
        for item in command.participants
    ]
    job.locations = _locations_from_command(command.locations)
    session.add(job)
    await session.flush()
    _record_job_created(session, job)
    access_links = tuple(
        [await issue_access_link(session, participant) for participant in job.participants]
    )
    return MoveJobCreatedResponse(job=_to_response(job), access_links=access_links)


async def create_customer_move_job(
    session: AsyncSession,
    command: CustomerMoveJobCreate,
) -> CustomerMoveJobCreatedResponse:
    """Create a customer-owned job without pre-issuing another role's access."""

    job = MoveJob(title=command.title, scheduled_at=command.scheduled_at)
    customer = JobParticipant(
        role=ParticipantRole.CUSTOMER,
        display_name=command.customer_display_name,
    )
    job.participants = [customer]
    job.locations = _locations_from_command(command.locations)
    session.add(job)
    await session.flush()
    _record_job_created(session, job)
    access_link = await issue_access_link(session, customer)
    return CustomerMoveJobCreatedResponse(
        job=_to_response(job),
        customer_access_link=access_link,
    )


async def get_move_job(session: AsyncSession, job_id: UUID) -> MoveJobResponse:
    """Return one complete topology without exposing ORM objects."""

    job = await _load_move_job(session, job_id)
    if job is None:
        raise MoveJobNotFoundError(job_id)
    return _to_response(job)


async def cancel_move_job(
    session: AsyncSession,
    job_id: UUID,
    actor_participant_id: UUID,
) -> None:
    """Cancel an unquoted job and revoke every capability without deleting history."""

    job = await session.scalar(select(MoveJob).where(MoveJob.id == job_id).with_for_update())
    if job is None:
        raise MoveJobNotFoundError(job_id)
    if job.status is MoveJobStatus.CANCELED:
        return
    if job.status is MoveJobStatus.COMPLETED:
        raise MoveJobConflictError(job_id)
    quote_id = await session.scalar(
        select(ScopeProposal.id).where(ScopeProposal.job_id == job_id).limit(1)
    )
    if quote_id is not None:
        raise MoveJobConflictError(job_id)

    now = utc_now()
    invitations = tuple(
        (
            await session.scalars(
                select(ParticipantInvitation)
                .where(ParticipantInvitation.job_id == job_id)
                .order_by(ParticipantInvitation.created_at, ParticipantInvitation.id)
                .with_for_update()
            )
        ).all()
    )
    for invitation in invitations:
        invitation.status = InvitationStatus.REVOKED
        invitation.resolved_at = now

    active_links = tuple(
        (
            await session.scalars(
                select(ParticipantAccessToken)
                .join(JobParticipant)
                .where(
                    JobParticipant.job_id == job_id,
                    ParticipantAccessToken.revoked_at.is_(None),
                )
                .options(selectinload(ParticipantAccessToken.participant))
                .order_by(ParticipantAccessToken.created_at, ParticipantAccessToken.id)
                .with_for_update()
            )
        ).all()
    )
    job.status = MoveJobStatus.CANCELED
    for access_link in active_links:
        await revoke_access_link(
            session,
            access_link,
            actor_participant_id,
            operation="job_canceled",
        )
    await session.flush()
