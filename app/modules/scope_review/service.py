"""Application commands and screen view for the quoted scope workflow."""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.contracts.actor import ParticipantRole
from app.contracts.ai import AnalysisResult, DraftItem
from app.contracts.media import MediaAssetStatus
from app.contracts.ports import (
    ProviderError,
    ProviderErrorKind,
    StoragePort,
)
from app.contracts.primitives import utc_now
from app.modules.capture.models import CaptureSession, MediaAsset
from app.modules.capture.service import STORAGE_TIMEOUT_SECONDS
from app.modules.move_job.models import (
    JobParticipant,
    Location,
    LocationKind,
    MoveJob,
)
from app.modules.scope.models import ScopeApproval, ScopeVersion
from app.modules.scope.schemas import ScopeContent, ScopeVersionCreate
from app.modules.scope.service import approve_scope_version, create_scope_version
from app.modules.scope_review.models import (
    ScopeProposal,
    ScopeProposalKind,
    ScopeProposalStatus,
    ScopeRevisionRequest,
)
from app.modules.scope_review.schemas import (
    QuoteSnapshot,
    RoomScopeGroup,
    ScopeConfirmResponse,
    ScopeMediaPreview,
    ScopeProposalCreate,
    ScopeProposalResponse,
    ScopeReviewItem,
    ScopeReviewJobHeader,
    ScopeReviewScope,
    ScopeReviewStatus,
    ScopeReviewView,
    ScopeRevisionRequestCreate,
    ScopeRevisionRequestResponse,
)

READ_URL_TTL_SECONDS = 5 * 60
MAX_MEDIA_PREVIEWS = 24


class ScopeReviewNotFoundError(LookupError):
    """Raised when a job-scoped scope review resource is absent."""


class ScopeReviewConflictError(ValueError):
    """Raised when a scope review command is stale or violates its lifecycle."""


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _normalized_content(content: ScopeContent) -> ScopeContent:
    return ScopeContent(
        schema_version=content.schema_version,
        items=tuple(sorted(content.items, key=lambda item: item.item_key)),
    )


def _quote_from_proposal(proposal: ScopeProposal) -> QuoteSnapshot:
    return QuoteSnapshot.model_validate(
        {
            "base_amount_krw": proposal.base_amount_krw,
            "adjustments": proposal.adjustments,
            "total_amount_krw": proposal.total_amount_krw,
        },
        strict=False,
    )


def _proposal_response(proposal: ScopeProposal) -> ScopeProposalResponse:
    return ScopeProposalResponse(
        proposal_id=proposal.id,
        proposal_kind=proposal.kind,
        status=proposal.status,
        source_scope_version_id=proposal.source_scope_version_id,
        result_scope_version_id=proposal.result_scope_version_id,
        quote=_quote_from_proposal(proposal),
        included_works=tuple(proposal.included_works),
        exclusions=tuple(proposal.exclusions),
        reason=proposal.reason,
        sent_at=_aware(proposal.sent_at),
        confirmed_at=(_aware(proposal.confirmed_at) if proposal.confirmed_at is not None else None),
    )


def _revision_response(request: ScopeRevisionRequest) -> ScopeRevisionRequestResponse:
    return ScopeRevisionRequestResponse(
        revision_request_id=request.id,
        scope_proposal_id=request.scope_proposal_id,
        scope_version_id=request.scope_version_id,
        status=("resolved" if request.resolved_at is not None else "requested"),
        reason=request.reason,
        requested_at=_aware(request.requested_at),
        resolved_by_scope_proposal_id=request.resolved_by_scope_proposal_id,
        resolved_at=_aware(request.resolved_at) if request.resolved_at is not None else None,
    )


async def _load_job(
    session: AsyncSession,
    job_id: UUID,
) -> MoveJob:
    job = await session.scalar(
        select(MoveJob)
        .where(MoveJob.id == job_id)
        .options(
            selectinload(MoveJob.participants),
            selectinload(MoveJob.locations).selectinload(Location.room_zones),
        )
    )
    if job is None:
        raise ScopeReviewNotFoundError(job_id)
    return job


async def _current_scope_version(
    session: AsyncSession,
    job_id: UUID,
) -> ScopeVersion:
    version = await session.scalar(
        select(ScopeVersion)
        .where(ScopeVersion.job_id == job_id)
        .order_by(ScopeVersion.sequence_number.desc(), ScopeVersion.id.desc())
        .limit(1)
    )
    if version is None:
        raise ScopeReviewNotFoundError(job_id)
    return version


async def _proposal_for_result(
    session: AsyncSession,
    job_id: UUID,
    scope_version_id: UUID,
    *,
    lock: bool = False,
) -> ScopeProposal | None:
    statement = select(ScopeProposal).where(
        ScopeProposal.job_id == job_id,
        ScopeProposal.result_scope_version_id == scope_version_id,
    )
    if lock:
        statement = statement.with_for_update()
    return (await session.scalars(statement)).one_or_none()


async def _revision_for_proposal(
    session: AsyncSession,
    proposal_id: UUID,
    *,
    lock: bool = False,
) -> ScopeRevisionRequest | None:
    statement = select(ScopeRevisionRequest).where(
        ScopeRevisionRequest.scope_proposal_id == proposal_id
    )
    if lock:
        statement = statement.with_for_update()
    return (await session.scalars(statement)).one_or_none()


def _proposal_matches_replay(
    proposal: ScopeProposal,
    result: ScopeVersion,
    participant_id: UUID,
    command: ScopeProposalCreate,
) -> bool:
    stored_content = ScopeContent.model_validate(result.content, strict=False)
    return (
        proposal.proposed_by_participant_id == participant_id
        and stored_content == _normalized_content(command.content)
        and _quote_from_proposal(proposal) == command.quote
        and tuple(proposal.included_works) == command.included_works
        and tuple(proposal.exclusions) == command.exclusions
        and proposal.reason == command.reason
    )


async def create_scope_proposal(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
    command: ScopeProposalCreate,
) -> ScopeProposalResponse:
    """Create one immutable quoted child scope, replaying an identical request safely."""

    source = await session.scalar(
        select(ScopeVersion)
        .where(
            ScopeVersion.id == command.source_scope_version_id,
            ScopeVersion.job_id == job_id,
        )
        .with_for_update()
    )
    if source is None:
        raise ScopeReviewNotFoundError(command.source_scope_version_id)

    existing = await session.scalar(
        select(ScopeProposal)
        .where(ScopeProposal.source_scope_version_id == source.id)
        .with_for_update()
    )
    if existing is not None:
        existing_result = await session.get(ScopeVersion, existing.result_scope_version_id)
        if existing_result is not None and _proposal_matches_replay(
            existing,
            existing_result,
            participant_id,
            command,
        ):
            return _proposal_response(existing)
        raise ScopeReviewConflictError(source.id)

    if source.locked_at is not None or await session.scalar(
        select(ScopeVersion.id).where(ScopeVersion.parent_version_id == source.id)
    ):
        raise ScopeReviewConflictError(source.id)

    previous = await _proposal_for_result(session, job_id, source.id, lock=True)
    revision: ScopeRevisionRequest | None = None
    if previous is None:
        creator_role = await session.scalar(
            select(JobParticipant.role).where(
                JobParticipant.id == source.created_by_participant_id,
                JobParticipant.job_id == job_id,
            )
        )
        if creator_role is not ParticipantRole.CUSTOMER:
            raise ScopeReviewConflictError(source.id)
        proposal_kind = ScopeProposalKind.INITIAL
    else:
        if previous.status is not ScopeProposalStatus.REVISION_REQUESTED:
            raise ScopeReviewConflictError(source.id)
        revision = await _revision_for_proposal(session, previous.id, lock=True)
        if revision is None or revision.resolved_at is not None:
            raise ScopeReviewConflictError(source.id)
        proposal_kind = ScopeProposalKind.REVISION

    try:
        created_version = await create_scope_version(
            session,
            job_id,
            participant_id,
            ScopeVersionCreate(
                parent_version_id=source.id,
                content=_normalized_content(command.content),
            ),
        )
        proposal = ScopeProposal(
            job_id=job_id,
            source_scope_version_id=source.id,
            result_scope_version_id=created_version.id,
            proposed_by_participant_id=participant_id,
            kind=proposal_kind,
            status=ScopeProposalStatus.CUSTOMER_REVIEW,
            base_amount_krw=command.quote.base_amount_krw,
            adjustments=[
                adjustment.model_dump(mode="json") for adjustment in command.quote.adjustments
            ],
            total_amount_krw=command.quote.total_amount_krw,
            included_works=list(command.included_works),
            exclusions=list(command.exclusions),
            reason=command.reason,
            sent_at=utc_now(),
        )
        session.add(proposal)
        await session.flush()
        await approve_scope_version(
            session,
            job_id,
            created_version.id,
            participant_id,
            ParticipantRole.COMPANY_MANAGER,
        )
        if previous is not None and revision is not None:
            now = utc_now()
            previous.status = ScopeProposalStatus.SUPERSEDED
            revision.resolved_by_scope_proposal_id = proposal.id
            revision.resolved_at = now
        await session.flush()
    except IntegrityError as error:
        raise ScopeReviewConflictError(source.id) from error
    return _proposal_response(proposal)


async def request_scope_revision(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
    command: ScopeRevisionRequestCreate,
) -> ScopeRevisionRequestResponse:
    """Request a replacement for the exact quoted version under customer review."""

    proposal = await _proposal_for_result(
        session,
        job_id,
        command.scope_version_id,
        lock=True,
    )
    if proposal is None:
        raise ScopeReviewNotFoundError(command.scope_version_id)
    existing = await _revision_for_proposal(session, proposal.id, lock=True)
    if existing is not None:
        if (
            existing.requested_by_participant_id == participant_id
            and existing.reason == command.reason
        ):
            return _revision_response(existing)
        raise ScopeReviewConflictError(proposal.id)
    if proposal.status is not ScopeProposalStatus.CUSTOMER_REVIEW or await session.scalar(
        select(ScopeVersion.id).where(ScopeVersion.parent_version_id == command.scope_version_id)
    ):
        raise ScopeReviewConflictError(proposal.id)

    request = ScopeRevisionRequest(
        job_id=job_id,
        scope_proposal_id=proposal.id,
        scope_version_id=command.scope_version_id,
        requested_by_participant_id=participant_id,
        reason=command.reason,
        requested_at=utc_now(),
    )
    session.add(request)
    proposal.status = ScopeProposalStatus.REVISION_REQUESTED
    try:
        await session.flush()
    except IntegrityError as error:
        raise ScopeReviewConflictError(proposal.id) from error
    return _revision_response(request)


async def confirm_scope_proposal(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
    scope_version_id: UUID,
    *,
    trace_id: str,
) -> ScopeConfirmResponse:
    """Add the customer's confirmation to the current company-confirmed version."""

    proposal = await _proposal_for_result(session, job_id, scope_version_id, lock=True)
    if proposal is None:
        raise ScopeReviewNotFoundError(scope_version_id)
    customer_approval = await session.scalar(
        select(ScopeApproval).where(
            ScopeApproval.scope_version_id == scope_version_id,
            ScopeApproval.role == ParticipantRole.CUSTOMER,
        )
    )
    if proposal.status is ScopeProposalStatus.CONFIRMED and customer_approval is not None:
        return ScopeConfirmResponse(
            proposal_id=proposal.id,
            scope_version_id=scope_version_id,
            confirmed_at=_aware(customer_approval.approved_at),
        )
    if proposal.status is not ScopeProposalStatus.CUSTOMER_REVIEW:
        raise ScopeReviewConflictError(proposal.id)

    approval = await approve_scope_version(
        session,
        job_id,
        scope_version_id,
        participant_id,
        ParticipantRole.CUSTOMER,
        trace_id=trace_id,
    )
    proposal.status = ScopeProposalStatus.CONFIRMED
    proposal.confirmed_at = approval.approval.approved_at
    await session.flush()
    return ScopeConfirmResponse(
        proposal_id=proposal.id,
        scope_version_id=scope_version_id,
        confirmed_at=_aware(approval.approval.approved_at),
    )


def _validated_read_url(value: str) -> str:
    try:
        if value != value.strip():
            raise ValueError
        parsed = urlsplit(value)
        _ = parsed.port
        if parsed.scheme.lower() != "https" or parsed.hostname is None:
            raise ValueError
    except ValueError:
        raise ProviderError(
            ProviderErrorKind.UNAVAILABLE,
            "storage returned an invalid read URL",
            retryable=False,
        ) from None
    return value


async def _media_previews(
    session: AsyncSession,
    storage: StoragePort,
    job_id: UUID,
    media_ids: tuple[UUID, ...],
) -> tuple[ScopeMediaPreview, ...]:
    if not media_ids:
        return ()
    assets = (
        await session.scalars(
            select(MediaAsset)
            .join(CaptureSession, CaptureSession.id == MediaAsset.capture_session_id)
            .where(
                CaptureSession.job_id == job_id,
                MediaAsset.id.in_(media_ids[:MAX_MEDIA_PREVIEWS]),
                MediaAsset.status == MediaAssetStatus.READY,
                MediaAsset.generation.is_not(None),
            )
            .order_by(MediaAsset.created_at, MediaAsset.id)
        )
    ).all()
    expires_at = utc_now() + timedelta(seconds=READ_URL_TTL_SECONDS)
    previews: list[ScopeMediaPreview] = []
    for asset in assets:
        assert asset.generation is not None
        read_url = await storage.create_read_url(
            object_key=asset.object_key,
            generation=asset.generation,
            expires_in_seconds=READ_URL_TTL_SECONDS,
            timeout_seconds=STORAGE_TIMEOUT_SECONDS,
        )
        previews.append(
            ScopeMediaPreview(
                media_asset_id=asset.id,
                room_zone_id=asset.room_zone_id,
                content_type=asset.content_type,
                read_url=_validated_read_url(read_url),
                expires_at=expires_at,
            )
        )
    return tuple(previews)


async def get_scope_review(
    session: AsyncSession,
    storage: StoragePort,
    job_id: UUID,
    participant_id: UUID,
    role: ParticipantRole,
) -> ScopeReviewView:
    """Compose the current immutable scope and quote into one frontend screen view."""

    job = await _load_job(session, job_id)
    viewer = next(
        (participant for participant in job.participants if participant.id == participant_id),
        None,
    )
    if viewer is None or viewer.role is not role:
        raise ScopeReviewNotFoundError(job_id)
    current = await _current_scope_version(session, job_id)
    versions = (
        await session.scalars(
            select(ScopeVersion)
            .where(ScopeVersion.job_id == job_id)
            .order_by(ScopeVersion.sequence_number)
        )
    ).all()
    by_id = {version.id: version for version in versions}
    analysis: AnalysisResult | None = None
    cursor: ScopeVersion | None = current
    while cursor is not None:
        if cursor.analysis_source is not None:
            analysis = AnalysisResult.model_validate(cursor.analysis_source, strict=False)
            break
        cursor = by_id.get(cursor.parent_version_id) if cursor.parent_version_id else None

    ai_items: dict[str, DraftItem] = {}
    review_required_keys: set[str] = set()
    if analysis is not None:
        ai_items = {
            item.item_key: item for item in analysis.draft_items + analysis.review_required_items
        }
        review_required_keys = {item.item_key for item in analysis.review_required_items}

    locations = sorted(job.locations, key=lambda location: location.kind.value)
    zone_order: dict[UUID, tuple[int, int, UUID]] = {}
    zone_labels: dict[UUID, str] = {}
    for location_index, location in enumerate(locations):
        for zone in location.room_zones:
            zone_order[zone.id] = (location_index, zone.sort_order, zone.id)
            zone_labels[zone.id] = zone.name

    content = ScopeContent.model_validate(current.content, strict=False)
    items_by_zone: defaultdict[UUID, list[ScopeReviewItem]] = defaultdict(list)
    media_ids: list[UUID] = []
    for item in content.items:
        if item.room_zone_id not in zone_labels:
            raise ScopeReviewConflictError(current.id)
        source_item = ai_items.get(item.item_key)
        source_media_ids = (
            tuple(UUID(str(value)) for value in source_item.source_media_asset_ids)
            if source_item is not None
            else ()
        )
        media_ids.extend(source_media_ids)
        items_by_zone[item.room_zone_id].append(
            ScopeReviewItem(
                item_key=item.item_key,
                room_zone_id=item.room_zone_id,
                description=item.description,
                review_required=item.item_key in review_required_keys,
                source_media_asset_ids=source_media_ids,
            )
        )

    room_groups = tuple(
        RoomScopeGroup(
            room_zone_id=zone_id,
            label=zone_labels[zone_id],
            item_count=len(items_by_zone[zone_id]),
            review_required_count=sum(item.review_required for item in items_by_zone[zone_id]),
            items=tuple(items_by_zone[zone_id]),
        )
        for zone_id in sorted(items_by_zone, key=zone_order.__getitem__)
    )
    proposal = await _proposal_for_result(session, job_id, current.id)
    revision = await _revision_for_proposal(session, proposal.id) if proposal is not None else None
    if proposal is None:
        review_status = ScopeReviewStatus.COMPANY_REVIEW
        included_works: tuple[str, ...] = ()
        exclusions: tuple[str, ...] = ()
    elif proposal.status is ScopeProposalStatus.CUSTOMER_REVIEW:
        review_status = ScopeReviewStatus.CUSTOMER_REVIEW
        included_works = tuple(proposal.included_works)
        exclusions = tuple(proposal.exclusions)
    elif proposal.status is ScopeProposalStatus.REVISION_REQUESTED:
        review_status = ScopeReviewStatus.REVISION_REQUESTED
        included_works = tuple(proposal.included_works)
        exclusions = tuple(proposal.exclusions)
    elif proposal.status is ScopeProposalStatus.CONFIRMED:
        review_status = ScopeReviewStatus.CONFIRMED
        included_works = tuple(proposal.included_works)
        exclusions = tuple(proposal.exclusions)
    else:
        raise ScopeReviewConflictError(proposal.id)

    approval_rows = (
        await session.execute(
            select(ScopeApproval.role, ScopeApproval.approved_at).where(
                ScopeApproval.scope_version_id == current.id
            )
        )
    ).tuples()
    approval_times = {stored_role: _aware(at) for stored_role, at in approval_rows}
    participant_names = {
        participant.role: participant.display_name for participant in job.participants
    }
    location_names = {location.kind: location.label for location in job.locations}
    unique_media_ids = tuple(dict.fromkeys(media_ids))
    return ScopeReviewView(
        job=ScopeReviewJobHeader(
            job_id=job.id,
            job_code=f"MOVE-{job.id.hex[:8].upper()}",
            title=job.title,
            scheduled_at=job.scheduled_at,
            customer_display_name=participant_names.get(ParticipantRole.CUSTOMER),
            company_display_name=participant_names.get(ParticipantRole.COMPANY_MANAGER),
            viewer_display_name=viewer.display_name,
            viewer_role=viewer.role,
            origin_summary=location_names.get(LocationKind.ORIGIN),
            destination_summary=location_names.get(LocationKind.DESTINATION),
        ),
        scope=ScopeReviewScope(
            id=current.id,
            version_label=f"v{current.sequence_number}",
            status=review_status,
            item_count=len(content.items),
            work_count=len(included_works),
            exclusion_count=len(exclusions),
            review_required_count=sum(
                item.review_required for group in room_groups for item in group.items
            ),
            room_groups=room_groups,
            included_works=included_works,
            exclusions=exclusions,
        ),
        proposal_id=proposal.id if proposal is not None else None,
        quote=_quote_from_proposal(proposal) if proposal is not None else None,
        proposal_reason=proposal.reason if proposal is not None else None,
        media_previews=await _media_previews(
            session,
            storage,
            job_id,
            unique_media_ids,
        ),
        company_confirmed_at=approval_times.get(ParticipantRole.COMPANY_MANAGER),
        customer_confirmed_at=approval_times.get(ParticipantRole.CUSTOMER),
        revision_request=_revision_response(revision) if revision is not None else None,
    )
