"""Application commands for move job topology."""

from base64 import b64decode, urlsafe_b64encode
from binascii import Error as BinasciiError
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.contracts.actor import ParticipantRole
from app.contracts.primitives import utc_now
from app.contracts.scope_review import (
    CompanyParticipationStatus,
    QuoteSnapshot,
    ScopeReviewStatus,
)
from app.modules.access.models import (
    InvitationStatus,
    ParticipantAccessToken,
    ParticipantInvitation,
    WorkspaceAccount,
    WorkspaceMembership,
)
from app.modules.access.service import issue_access_link, revoke_access_link
from app.modules.completion.models import AuditEventType, CompletionRequest, CompletionRequestStatus
from app.modules.completion.service import add_audit_event
from app.modules.field_change.models import ChangeProposalDetail
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
    MoveJobListResponse,
    MoveJobPatch,
    MoveJobResponse,
    MoveJobSummaryResponse,
    ParticipantResponse,
    RoomZoneResponse,
)
from app.modules.scope.models import ChangeRequest, ChangeRequestStatus, ScopeVersion
from app.modules.scope.schemas import ScopeContent, ScopeVersionCreate
from app.modules.scope.service import ScopeVersionConflictError, create_scope_version
from app.modules.scope_review.models import ScopeProposal, ScopeProposalStatus

_MAX_LIST_CURSOR_OFFSET = 2**63 - 1


class MoveJobNotFoundError(LookupError):
    """Raised when a move job does not exist."""


class MoveJobConflictError(RuntimeError):
    """Raised when a move job can no longer accept the requested mutation."""


def _locations_from_command(locations: tuple[LocationCreate, ...]) -> list[Location]:
    """Build owned location rows from a validated creation command."""

    return [
        Location(
            kind=item.kind,
            label=item.label,
            detail_address=item.detail_address,
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


def _to_response(
    job: MoveJob,
    *,
    viewer_role: ParticipantRole,
) -> MoveJobResponse:
    expose_detail_address = viewer_role in {
        ParticipantRole.CUSTOMER,
        ParticipantRole.COMPANY_MANAGER,
    }
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
                detail_address=(location.detail_address if expose_detail_address else None),
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
    return MoveJobCreatedResponse(
        job=_to_response(job, viewer_role=ParticipantRole.CUSTOMER),
        access_links=access_links,
    )


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
        job=_to_response(job, viewer_role=ParticipantRole.CUSTOMER),
        customer_access_link=access_link,
        connection_code=f"MOVE-{job.id.hex[:8].upper()}",
    )


async def get_move_job(
    session: AsyncSession,
    job_id: UUID,
    *,
    viewer_role: ParticipantRole,
) -> MoveJobResponse:
    """Return one complete topology without exposing ORM objects."""

    job = await _load_move_job(session, job_id)
    if job is None:
        raise MoveJobNotFoundError(job_id)
    return _to_response(job, viewer_role=viewer_role)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _encode_list_cursor(offset: int) -> str:
    """Encode the next offset without exposing implementation details to clients."""

    payload = f"v1:{offset}".encode("ascii")
    return urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_list_cursor(cursor: str) -> int:
    """Validate and decode an opaque move-list cursor."""

    if not cursor or len(cursor) > 256:
        raise ValueError("cursor is invalid")
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = b64decode(
            f"{cursor}{padding}".encode("ascii"),
            altchars=b"-_",
            validate=True,
        ).decode("ascii")
        version, offset_value = payload.split(":", maxsplit=1)
        if version != "v1" or not offset_value.isascii() or not offset_value.isdecimal():
            raise ValueError("cursor is invalid")
        offset = int(offset_value)
    except (BinasciiError, UnicodeDecodeError, ValueError) as error:
        raise ValueError("cursor is invalid") from error
    if offset < 0 or offset > _MAX_LIST_CURSOR_OFFSET:
        raise ValueError("cursor is invalid")
    return offset


async def _company_status(
    session: AsyncSession,
    job: MoveJob,
) -> CompanyParticipationStatus:
    invitation = await session.scalar(
        select(ParticipantInvitation).where(
            ParticipantInvitation.job_id == job.id,
            ParticipantInvitation.role == ParticipantRole.COMPANY_MANAGER,
        )
    )

    return _company_status_from_invitation(job, invitation)


def _company_status_from_invitation(
    job: MoveJob,
    invitation: ParticipantInvitation | None,
) -> CompanyParticipationStatus:
    company_exists = any(
        participant.role is ParticipantRole.COMPANY_MANAGER for participant in job.participants
    )
    if invitation is None:
        return (
            CompanyParticipationStatus.JOINED
            if company_exists
            else CompanyParticipationStatus.NOT_INVITED
        )
    if invitation.status is InvitationStatus.PENDING:
        return (
            CompanyParticipationStatus.EXPIRED
            if _aware(invitation.expires_at) <= utc_now()
            else CompanyParticipationStatus.INVITED
        )
    return {
        InvitationStatus.ACCEPTED: CompanyParticipationStatus.JOINED,
        InvitationStatus.DECLINED: CompanyParticipationStatus.DECLINED,
        InvitationStatus.EXPIRED: CompanyParticipationStatus.EXPIRED,
        InvitationStatus.REVOKED: CompanyParticipationStatus.REVOKED,
    }[invitation.status]


def _scope_status(proposal: ScopeProposal | None) -> ScopeReviewStatus:
    if proposal is None:
        return ScopeReviewStatus.COMPANY_REVIEW
    return {
        ScopeProposalStatus.CUSTOMER_REVIEW: ScopeReviewStatus.CUSTOMER_REVIEW,
        ScopeProposalStatus.REVISION_REQUESTED: ScopeReviewStatus.REVISION_REQUESTED,
        ScopeProposalStatus.CONFIRMED: ScopeReviewStatus.CONFIRMED,
        ScopeProposalStatus.SUPERSEDED: ScopeReviewStatus.COMPANY_REVIEW,
    }[proposal.status]


async def _move_summary(
    session: AsyncSession,
    job: MoveJob,
    *,
    viewer_role: ParticipantRole,
) -> MoveJobSummaryResponse:
    scope_version = await session.scalar(
        select(ScopeVersion)
        .where(ScopeVersion.job_id == job.id)
        .order_by(ScopeVersion.sequence_number.desc(), ScopeVersion.id.desc())
        .limit(1)
    )
    proposal = await session.scalar(
        select(ScopeProposal)
        .where(ScopeProposal.job_id == job.id)
        .order_by(ScopeProposal.sent_at.desc(), ScopeProposal.id.desc())
        .limit(1)
    )
    completion_status = await session.scalar(
        select(CompletionRequest.status)
        .where(CompletionRequest.job_id == job.id)
        .order_by(CompletionRequest.requested_at.desc(), CompletionRequest.id.desc())
        .limit(1)
    )
    current_change = None
    if scope_version is not None:
        current_change = await session.scalar(
            select(ChangeProposalDetail)
            .join(
                ChangeRequest,
                ChangeRequest.id == ChangeProposalDetail.change_request_id,
            )
            .where(
                ChangeRequest.job_id == job.id,
                ChangeRequest.status == ChangeRequestStatus.APPROVED,
                ChangeRequest.result_scope_version_id == scope_version.id,
            )
            .limit(1)
        )
    return _move_summary_response(
        job,
        scope_version=scope_version,
        proposal=proposal,
        completion_status=completion_status,
        current_change=current_change,
        company_status=await _company_status(session, job),
        viewer_role=viewer_role,
    )


def _move_summary_response(
    job: MoveJob,
    *,
    scope_version: ScopeVersion | None,
    proposal: ScopeProposal | None,
    completion_status: CompletionRequestStatus | None,
    current_change: ChangeProposalDetail | None,
    company_status: CompanyParticipationStatus,
    viewer_role: ParticipantRole,
) -> MoveJobSummaryResponse:
    quote = None
    adjustment_count = 0
    if current_change is not None:
        quote = QuoteSnapshot.model_validate(
            {
                "base_amount_krw": current_change.base_amount_krw,
                "adjustments": current_change.adjustments,
                "total_amount_krw": current_change.total_amount_krw,
            },
            strict=False,
        )
        adjustment_count = len(quote.adjustments)
    elif proposal is not None:
        quote = QuoteSnapshot.model_validate(
            {
                "base_amount_krw": proposal.base_amount_krw,
                "adjustments": proposal.adjustments,
                "total_amount_krw": proposal.total_amount_krw,
            },
            strict=False,
        )
        adjustment_count = len(quote.adjustments)
    content = scope_version.content if scope_version is not None else {}
    raw_items = content.get("items", [])
    item_count = len(raw_items) if isinstance(raw_items, list) else 0
    return MoveJobSummaryResponse(
        job=_to_response(job, viewer_role=viewer_role),
        version_label=(
            f"V{scope_version.sequence_number}" if scope_version is not None else "초안"
        ),
        scope_status=_scope_status(proposal),
        company_participation_status=company_status,
        completion_request_status=completion_status,
        quote=quote,
        item_count=item_count,
        adjustment_count=adjustment_count,
    )


async def list_move_jobs(
    session: AsyncSession,
    *,
    actor_participant_id: UUID | None = None,
    account_id: UUID | None = None,
    status_filter: MoveJobStatus | None = None,
    search: str | None = None,
    scheduled_from: datetime | None = None,
    scheduled_to: datetime | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> MoveJobListResponse:
    """List every job attached to one capability or durable workspace."""

    if (actor_participant_id is None) == (account_id is None):
        raise ValueError("exactly one list identity is required")
    if scheduled_from is not None and scheduled_from.utcoffset() is None:
        raise ValueError("scheduled_from must include a timezone")
    if scheduled_to is not None and scheduled_to.utcoffset() is None:
        raise ValueError("scheduled_to must include a timezone")
    if scheduled_from is not None and scheduled_to is not None and scheduled_from > scheduled_to:
        raise ValueError("scheduled_from must not exceed scheduled_to")
    if search is not None and not search.strip():
        raise ValueError("q must contain a non-whitespace character")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    cursor_offset = _decode_list_cursor(cursor) if cursor is not None else 0

    if actor_participant_id is not None:
        viewer_role = await session.scalar(
            select(JobParticipant.role).where(JobParticipant.id == actor_participant_id)
        )
    else:
        viewer_role = await session.scalar(
            select(WorkspaceAccount.role).where(WorkspaceAccount.id == account_id)
        )
    if viewer_role is None:
        raise ValueError("list identity is invalid")

    participant_job_ids = select(JobParticipant.job_id)
    if account_id is not None:
        participant_job_ids = participant_job_ids.join(
            WorkspaceMembership,
            WorkspaceMembership.participant_id == JobParticipant.id,
        ).where(
            WorkspaceMembership.account_id == account_id,
            WorkspaceMembership.revoked_at.is_(None),
        )
    else:
        participant_job_ids = participant_job_ids.where(JobParticipant.id == actor_participant_id)
    statement = (
        select(MoveJob)
        .where(MoveJob.id.in_(participant_job_ids))
        .options(
            selectinload(MoveJob.participants),
            selectinload(MoveJob.locations).selectinload(Location.room_zones),
        )
    )
    if status_filter is not None:
        statement = statement.where(MoveJob.status == status_filter)
    if scheduled_from is not None:
        statement = statement.where(MoveJob.scheduled_at >= scheduled_from)
    if scheduled_to is not None:
        statement = statement.where(MoveJob.scheduled_at <= scheduled_to)
    if search:
        pattern = f"%{search.strip().lower()}%"
        search_predicates = [
            func.lower(MoveJob.title).like(pattern),
            func.lower(Location.label).like(pattern),
            func.lower(JobParticipant.display_name).like(pattern),
        ]
        if viewer_role is not ParticipantRole.FIELD_WORKER:
            search_predicates.append(func.lower(Location.detail_address).like(pattern))
        matching_jobs = (
            select(MoveJob.id)
            .outerjoin(Location, Location.job_id == MoveJob.id)
            .outerjoin(JobParticipant, JobParticipant.job_id == MoveJob.id)
            .where(or_(*search_predicates))
        )
        statement = statement.where(MoveJob.id.in_(matching_jobs))
    fetched_jobs = (
        (
            await session.scalars(
                statement.order_by(MoveJob.created_at.desc(), MoveJob.id.desc())
                .offset(cursor_offset)
                .limit(limit + 1)
            )
        )
        .unique()
        .all()
    )
    has_more = len(fetched_jobs) > limit
    jobs = fetched_jobs[:limit]
    if not jobs:
        return MoveJobListResponse(moves=(), next_cursor=None)
    next_cursor = _encode_list_cursor(cursor_offset + len(jobs)) if has_more else None

    job_ids = tuple(job.id for job in jobs)
    scope_versions = (
        await session.scalars(
            select(ScopeVersion)
            .where(ScopeVersion.job_id.in_(job_ids))
            .order_by(
                ScopeVersion.job_id,
                ScopeVersion.sequence_number.desc(),
                ScopeVersion.id.desc(),
            )
        )
    ).all()
    scope_by_job: dict[UUID, ScopeVersion] = {}
    for scope_version in scope_versions:
        scope_by_job.setdefault(scope_version.job_id, scope_version)

    proposals = (
        await session.scalars(
            select(ScopeProposal)
            .where(ScopeProposal.job_id.in_(job_ids))
            .order_by(
                ScopeProposal.job_id,
                ScopeProposal.sent_at.desc(),
                ScopeProposal.id.desc(),
            )
        )
    ).all()
    proposal_by_job: dict[UUID, ScopeProposal] = {}
    for proposal in proposals:
        proposal_by_job.setdefault(proposal.job_id, proposal)

    completion_rows = (
        await session.execute(
            select(CompletionRequest.job_id, CompletionRequest.status)
            .where(CompletionRequest.job_id.in_(job_ids))
            .order_by(
                CompletionRequest.job_id,
                CompletionRequest.requested_at.desc(),
                CompletionRequest.id.desc(),
            )
        )
    ).all()
    completion_by_job: dict[UUID, CompletionRequestStatus] = {}
    for completion_job_id, completion_status in completion_rows:
        completion_by_job.setdefault(completion_job_id, completion_status)

    change_rows = (
        await session.execute(
            select(ChangeProposalDetail, ChangeRequest.result_scope_version_id)
            .join(
                ChangeRequest,
                ChangeRequest.id == ChangeProposalDetail.change_request_id,
            )
            .where(
                ChangeRequest.job_id.in_(job_ids),
                ChangeRequest.status == ChangeRequestStatus.APPROVED,
            )
        )
    ).all()
    change_by_scope: dict[UUID, ChangeProposalDetail] = {
        result_scope_version_id: detail
        for detail, result_scope_version_id in change_rows
        if result_scope_version_id is not None
    }

    invitations = (
        await session.scalars(
            select(ParticipantInvitation).where(
                ParticipantInvitation.job_id.in_(job_ids),
                ParticipantInvitation.role == ParticipantRole.COMPANY_MANAGER,
            )
        )
    ).all()
    invitation_by_job = {invitation.job_id: invitation for invitation in invitations}

    moves: list[MoveJobSummaryResponse] = []
    for job in jobs:
        current_scope_version = scope_by_job.get(job.id)
        moves.append(
            _move_summary_response(
                job,
                scope_version=current_scope_version,
                proposal=proposal_by_job.get(job.id),
                completion_status=completion_by_job.get(job.id),
                current_change=(
                    change_by_scope.get(current_scope_version.id)
                    if current_scope_version is not None
                    else None
                ),
                company_status=_company_status_from_invitation(
                    job,
                    invitation_by_job.get(job.id),
                ),
                viewer_role=viewer_role,
            )
        )
    return MoveJobListResponse(moves=tuple(moves), next_cursor=next_cursor)


async def patch_move_job(
    session: AsyncSession,
    job_id: UUID,
    actor_participant_id: UUID,
    command: MoveJobPatch,
) -> MoveJobResponse:
    """Update basic information while preserving any v2 scope snapshot history."""

    job = await session.scalar(
        select(MoveJob)
        .where(MoveJob.id == job_id)
        .options(
            selectinload(MoveJob.participants),
            selectinload(MoveJob.locations).selectinload(Location.room_zones),
        )
        .with_for_update()
    )
    if job is None:
        raise MoveJobNotFoundError(job_id)
    if job.status in {MoveJobStatus.COMPLETED, MoveJobStatus.CANCELED}:
        raise MoveJobConflictError(job_id)
    proposal_id = await session.scalar(
        select(ScopeProposal.id).where(ScopeProposal.job_id == job_id).limit(1)
    )
    if proposal_id is not None:
        raise MoveJobConflictError(job_id)

    current_scope = await session.scalar(
        select(ScopeVersion)
        .where(ScopeVersion.job_id == job_id)
        .order_by(ScopeVersion.sequence_number.desc(), ScopeVersion.id.desc())
        .limit(1)
    )
    changes_scope_conditions = any(
        location_patch.conditions is not None for location_patch in command.locations or ()
    )
    if (
        changes_scope_conditions
        and current_scope is not None
        and current_scope.locked_at is not None
    ):
        raise MoveJobConflictError(job_id)

    changed_fields: list[str] = []
    if "title" in command.model_fields_set:
        job.title = command.title or ""
        changed_fields.append("title")
    if "scheduled_at" in command.model_fields_set:
        job.scheduled_at = command.scheduled_at
        changed_fields.append("scheduled_at")
    if command.locations is not None:
        locations_by_kind = {location.kind: location for location in job.locations}
        for location_patch in command.locations:
            location = locations_by_kind.get(location_patch.kind)
            if location is None:
                raise MoveJobNotFoundError(job_id)
            if location_patch.label is not None:
                location.label = location_patch.label
            if "detail_address" in location_patch.model_fields_set:
                location.detail_address = location_patch.detail_address
            if location_patch.conditions is not None:
                location.conditions = location_patch.conditions.model_dump(mode="json")
            changed_fields.append(f"location:{location_patch.kind.value}")
    job.updated_at = utc_now()
    if (
        changes_scope_conditions
        and current_scope is not None
        and current_scope.content.get("schema_version", 1) == 2
    ):
        current_content = ScopeContent.model_validate(current_scope.content, strict=False)
        try:
            await create_scope_version(
                session,
                job_id,
                actor_participant_id,
                ScopeVersionCreate(
                    parent_version_id=current_scope.id,
                    content=current_content.model_copy(update={"location_conditions": ()}),
                ),
            )
        except ScopeVersionConflictError as error:
            raise MoveJobConflictError(job_id) from error
    add_audit_event(
        session,
        job.id,
        AuditEventType.JOB_BASIC_INFO_UPDATED,
        actor_participant_id=actor_participant_id,
        payload={"changed_fields": sorted(changed_fields)},
    )
    await session.flush()
    return _to_response(job, viewer_role=ParticipantRole.CUSTOMER)


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
