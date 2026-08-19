"""Application commands and screen view for the quoted scope workflow."""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
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
from app.modules.access.models import InvitationStatus, ParticipantInvitation
from app.modules.capture.models import CaptureSession, MediaAsset
from app.modules.capture.service import STORAGE_TIMEOUT_SECONDS
from app.modules.field_change.models import ChangeProposalDetail
from app.modules.move_job.models import (
    JobParticipant,
    Location,
    LocationKind,
    MoveJob,
    MoveJobStatus,
)
from app.modules.scope.models import (
    ChangeRequest,
    ChangeRequestEvidence,
    ChangeRequestStatus,
    ScopeApproval,
    ScopeVersion,
)
from app.modules.scope.schemas import (
    ScopeContent,
    ScopeItem,
    ScopeItemReviewStatus,
    ScopeItemSource,
    ScopeItemV2,
    ScopeVersionCreate,
)
from app.modules.scope.service import approve_scope_version, create_scope_version
from app.modules.scope_review.models import (
    ScopeProposal,
    ScopeProposalKind,
    ScopeProposalStatus,
    ScopeRevisionRequest,
)
from app.modules.scope_review.schemas import (
    ApprovedChangeSummary,
    CompanyParticipationStatus,
    ExecutionPlanSnapshot,
    QuoteSnapshot,
    RoomScopeGroup,
    ScopeCollaborationStatus,
    ScopeConfirmationHistoryEntry,
    ScopeConfirmationHistoryView,
    ScopeConfirmationRecord,
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
AGREEMENT_NOTICE = "전자계약이 아닌 소비자와 업체의 공동확인 기록입니다."


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
        location_conditions=tuple(
            sorted(
                content.location_conditions,
                key=lambda item: (item.kind.value, str(item.location_id)),
            )
        ),
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


def _quote_from_change_detail(detail: ChangeProposalDetail) -> QuoteSnapshot:
    return QuoteSnapshot.model_validate(
        {
            "base_amount_krw": detail.base_amount_krw,
            "adjustments": detail.adjustments,
            "total_amount_krw": detail.total_amount_krw,
        },
        strict=False,
    )


def _execution_plan_from_proposal(
    proposal: ScopeProposal,
) -> ExecutionPlanSnapshot | None:
    if proposal.execution_plan is None:
        return None
    return ExecutionPlanSnapshot.model_validate(proposal.execution_plan, strict=False)


def _proposal_response(proposal: ScopeProposal) -> ScopeProposalResponse:
    return ScopeProposalResponse(
        proposal_id=proposal.id,
        proposal_kind=proposal.kind,
        status=proposal.status,
        source_scope_version_id=proposal.source_scope_version_id,
        result_scope_version_id=proposal.result_scope_version_id,
        quote=_quote_from_proposal(proposal),
        execution_plan=ExecutionPlanSnapshot.model_validate(
            proposal.execution_plan,
            strict=False,
        ),
        included_works=tuple(proposal.included_works),
        exclusions=tuple(proposal.exclusions),
        reason=proposal.reason,
        sent_at=_aware(proposal.sent_at),
        confirmed_at=(_aware(proposal.confirmed_at) if proposal.confirmed_at is not None else None),
    )


async def _company_participation_status(
    session: AsyncSession,
    job: MoveJob,
) -> CompanyParticipationStatus:
    invitation = await session.scalar(
        select(ParticipantInvitation).where(
            ParticipantInvitation.job_id == job.id,
            ParticipantInvitation.role == ParticipantRole.COMPANY_MANAGER,
        )
    )
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
        if _aware(invitation.expires_at) <= utc_now():
            return CompanyParticipationStatus.EXPIRED
        return CompanyParticipationStatus.INVITED
    return {
        InvitationStatus.ACCEPTED: CompanyParticipationStatus.JOINED,
        InvitationStatus.DECLINED: CompanyParticipationStatus.DECLINED,
        InvitationStatus.EXPIRED: CompanyParticipationStatus.EXPIRED,
        InvitationStatus.REVOKED: CompanyParticipationStatus.REVOKED,
    }[invitation.status]


async def _approved_change_summaries(
    session: AsyncSession,
    job_id: UUID,
) -> tuple[ApprovedChangeSummary, ...]:
    rows = (
        (
            await session.execute(
                select(ChangeProposalDetail, ChangeRequest)
                .join(
                    ChangeRequest,
                    ChangeRequest.id == ChangeProposalDetail.change_request_id,
                )
                .where(
                    ChangeRequest.job_id == job_id,
                    ChangeRequest.status == ChangeRequestStatus.APPROVED,
                )
                .order_by(ChangeRequest.decided_at, ChangeRequest.id)
            )
        )
        .tuples()
        .all()
    )
    request_ids = tuple(request.id for _, request in rows)
    evidence_by_request: defaultdict[UUID, list[UUID]] = defaultdict(list)
    if request_ids:
        evidence_rows = (
            await session.execute(
                select(
                    ChangeRequestEvidence.change_request_id,
                    ChangeRequestEvidence.media_asset_id,
                )
                .where(ChangeRequestEvidence.change_request_id.in_(request_ids))
                .order_by(
                    ChangeRequestEvidence.change_request_id,
                    ChangeRequestEvidence.media_asset_id,
                )
            )
        ).tuples()
        for request_id, media_asset_id in evidence_rows:
            evidence_by_request[request_id].append(media_asset_id)
    summaries: list[ApprovedChangeSummary] = []
    for detail, request in rows:
        summaries.append(
            ApprovedChangeSummary(
                proposal_id=request.id,
                field_issue_id=detail.field_issue_id,
                title=detail.title,
                reason=request.description,
                base_scope_version_id=request.base_scope_version_id,
                result_scope_version_id=cast(UUID, request.result_scope_version_id),
                quote=_quote_from_change_detail(detail),
                evidence_media_asset_ids=tuple(evidence_by_request[request.id]),
                approved_at=_aware(cast(datetime, request.decided_at)),
            )
        )
    return tuple(summaries)


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


def _job_header(
    job: MoveJob,
    viewer: JobParticipant,
    company_participation: CompanyParticipationStatus,
) -> ScopeReviewJobHeader:
    participant_names = {
        participant.role: participant.display_name for participant in job.participants
    }
    location_names = {location.kind: location.label for location in job.locations}
    return ScopeReviewJobHeader(
        job_id=job.id,
        job_code=f"MOVE-{job.id.hex[:8].upper()}",
        title=job.title,
        scheduled_at=job.scheduled_at,
        customer_display_name=participant_names.get(ParticipantRole.CUSTOMER),
        company_display_name=(
            participant_names.get(ParticipantRole.COMPANY_MANAGER)
            if company_participation is CompanyParticipationStatus.JOINED
            else None
        ),
        viewer_display_name=viewer.display_name,
        viewer_role=viewer.role,
        origin_summary=location_names.get(LocationKind.ORIGIN),
        destination_summary=location_names.get(LocationKind.DESTINATION),
    )


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
        and (
            stored_content == _normalized_content(command.content)
            or (
                command.content.schema_version == 2
                and not command.content.location_conditions
                and stored_content.items == _normalized_content(command.content).items
            )
        )
        and _quote_from_proposal(proposal) == command.quote
        and _execution_plan_from_proposal(proposal) == command.execution_plan
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

    job_status = await session.scalar(
        select(MoveJob.status).where(MoveJob.id == job_id).with_for_update()
    )
    if job_status is None:
        raise ScopeReviewNotFoundError(job_id)
    if job_status in {MoveJobStatus.COMPLETED, MoveJobStatus.CANCELED}:
        raise ScopeReviewConflictError(job_id)

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
            execution_plan=command.execution_plan.model_dump(mode="json"),
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


async def get_scope_confirmation_history(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
    role: ParticipantRole,
) -> ScopeConfirmationHistoryView:
    """Return every immutable scope with its quote and role confirmation timeline."""

    job = await _load_job(session, job_id)
    viewer = next(
        (
            participant
            for participant in job.participants
            if participant.id == participant_id and participant.role is role
        ),
        None,
    )
    if viewer is None or role not in {
        ParticipantRole.CUSTOMER,
        ParticipantRole.COMPANY_MANAGER,
    }:
        raise ScopeReviewNotFoundError(job_id)

    versions = (
        await session.scalars(
            select(ScopeVersion)
            .where(ScopeVersion.job_id == job_id)
            .order_by(ScopeVersion.sequence_number, ScopeVersion.id)
        )
    ).all()
    version_ids = tuple(version.id for version in versions)
    proposals = (
        await session.scalars(
            select(ScopeProposal)
            .where(ScopeProposal.job_id == job_id)
            .order_by(ScopeProposal.sent_at, ScopeProposal.id)
        )
    ).all()
    proposal_by_result = {proposal.result_scope_version_id: proposal for proposal in proposals}
    change_rows = (
        (
            await session.execute(
                select(ChangeProposalDetail, ChangeRequest)
                .join(
                    ChangeRequest,
                    ChangeRequest.id == ChangeProposalDetail.change_request_id,
                )
                .where(
                    ChangeRequest.job_id == job_id,
                    ChangeRequest.status == ChangeRequestStatus.APPROVED,
                    ChangeRequest.result_scope_version_id.is_not(None),
                )
            )
        )
        .tuples()
        .all()
    )
    change_by_result = {
        cast(UUID, request.result_scope_version_id): (detail, request)
        for detail, request in change_rows
    }
    approval_rows = (
        (
            await session.execute(
                select(
                    ScopeApproval.scope_version_id,
                    ScopeApproval.participant_id,
                    ScopeApproval.role,
                    ScopeApproval.approved_at,
                )
                .where(ScopeApproval.scope_version_id.in_(version_ids))
                .order_by(
                    ScopeApproval.scope_version_id,
                    ScopeApproval.approved_at,
                    ScopeApproval.role,
                )
            )
        )
        .tuples()
        .all()
    )
    confirmations_by_version: defaultdict[UUID, list[ScopeConfirmationRecord]] = defaultdict(list)
    for scope_version_id, confirmed_participant_id, confirmed_role, confirmed_at in approval_rows:
        confirmations_by_version[scope_version_id].append(
            ScopeConfirmationRecord(
                participant_id=confirmed_participant_id,
                role=confirmed_role,
                confirmed_at=_aware(confirmed_at),
            )
        )

    agreement_by_version: dict[UUID, ScopeProposal | None] = {}
    entries: list[ScopeConfirmationHistoryEntry] = []
    for version in versions:
        proposal = proposal_by_result.get(version.id)
        change = change_by_result.get(version.id)
        inherited_agreement = (
            agreement_by_version.get(version.parent_version_id)
            if version.parent_version_id is not None
            else None
        )
        agreement = proposal if proposal is not None else inherited_agreement
        agreement_by_version[version.id] = agreement

        source: Literal["scope", "quote", "field_change"]
        if change is not None:
            detail, request = change
            source = "field_change"
            quote = _quote_from_change_detail(detail)
            proposal_id = request.id
            proposal_reason = request.description
        elif proposal is not None:
            source = "quote"
            quote = _quote_from_proposal(proposal)
            proposal_id = proposal.id
            proposal_reason = proposal.reason
        else:
            source = "scope"
            quote = None
            proposal_id = None
            proposal_reason = None

        confirmations = tuple(confirmations_by_version[version.id])
        confirmed_roles = {confirmation.role for confirmation in confirmations}
        entries.append(
            ScopeConfirmationHistoryEntry(
                scope_version_id=version.id,
                parent_scope_version_id=version.parent_version_id,
                sequence_number=version.sequence_number,
                version_label=f"v{version.sequence_number}",
                source=source,
                content=ScopeContent.model_validate(version.content, strict=False),
                content_hash=version.content_hash,
                quote=quote,
                included_works=(tuple(agreement.included_works) if agreement is not None else ()),
                exclusions=tuple(agreement.exclusions) if agreement is not None else (),
                proposal_id=proposal_id,
                proposal_reason=proposal_reason,
                confirmations=confirmations,
                bilaterally_confirmed={
                    ParticipantRole.CUSTOMER,
                    ParticipantRole.COMPANY_MANAGER,
                }.issubset(confirmed_roles),
                created_at=_aware(version.created_at),
                locked_at=_aware(version.locked_at) if version.locked_at is not None else None,
            )
        )

    company_participation = await _company_participation_status(session, job)
    return ScopeConfirmationHistoryView(
        job=_job_header(job, viewer, company_participation),
        versions=tuple(entries),
    )


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
    if role is ParticipantRole.FIELD_WORKER and current.locked_at is None:
        raise ScopeReviewNotFoundError(current.id)
    versions = (
        await session.scalars(
            select(ScopeVersion)
            .where(ScopeVersion.job_id == job_id)
            .order_by(ScopeVersion.sequence_number)
        )
    ).all()
    by_id = {version.id: version for version in versions}
    analysis: AnalysisResult | None = None
    lineage: list[ScopeVersion] = []
    cursor: ScopeVersion | None = current
    while cursor is not None:
        lineage.append(cursor)
        if analysis is None and cursor.analysis_source is not None:
            analysis = AnalysisResult.model_validate(cursor.analysis_source, strict=False)
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
        if isinstance(item, ScopeItemV2):
            description = item.name
            name = item.name
            quantity = item.quantity
            unit = item.unit
            work_note = item.work_note
            item_review_status = item.review_status
            item_source = item.source
        else:
            assert isinstance(item, ScopeItem)
            description = item.description
            name = item.description
            quantity = None
            unit = None
            work_note = None
            item_review_status = (
                ScopeItemReviewStatus.REVIEW_REQUIRED
                if item.item_key in review_required_keys
                else ScopeItemReviewStatus.CONFIRMED
            )
            item_source = (
                ScopeItemSource.AI if source_item is not None else ScopeItemSource.CUSTOMER
            )
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
                description=description,
                name=name,
                quantity=quantity,
                unit=unit,
                work_note=work_note,
                review_status=item_review_status,
                source=item_source,
                review_required=item_review_status is ScopeItemReviewStatus.REVIEW_REQUIRED,
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
    proposals = (
        await session.scalars(
            select(ScopeProposal)
            .where(ScopeProposal.job_id == job_id)
            .order_by(ScopeProposal.sent_at, ScopeProposal.id)
        )
    ).all()
    proposal_by_result = {stored.result_scope_version_id: stored for stored in proposals}
    proposal = await _proposal_for_result(session, job_id, current.id)
    agreement_proposal = proposal
    if agreement_proposal is None:
        agreement_proposal = next(
            (
                stored
                for version in lineage
                if (stored := proposal_by_result.get(version.id)) is not None
            ),
            None,
        )
    approved_changes = await _approved_change_summaries(session, job_id)
    current_change = next(
        (
            change
            for change in reversed(approved_changes)
            if change.result_scope_version_id == current.id
        ),
        None,
    )
    revision = await _revision_for_proposal(session, proposal.id) if proposal is not None else None
    if current_change is not None:
        review_status = ScopeReviewStatus.CONFIRMED
    elif proposal is None:
        review_status = ScopeReviewStatus.COMPANY_REVIEW
    elif proposal.status is ScopeProposalStatus.CUSTOMER_REVIEW:
        review_status = ScopeReviewStatus.CUSTOMER_REVIEW
    elif proposal.status is ScopeProposalStatus.REVISION_REQUESTED:
        review_status = ScopeReviewStatus.REVISION_REQUESTED
    elif proposal.status is ScopeProposalStatus.CONFIRMED:
        review_status = ScopeReviewStatus.CONFIRMED
    else:
        raise ScopeReviewConflictError(proposal.id)
    included_works = (
        tuple(agreement_proposal.included_works) if agreement_proposal is not None else ()
    )
    exclusions = tuple(agreement_proposal.exclusions) if agreement_proposal is not None else ()

    company_participation = await _company_participation_status(session, job)
    if review_status is ScopeReviewStatus.CONFIRMED:
        collaboration_status = ScopeCollaborationStatus.CONFIRMED
    elif review_status is ScopeReviewStatus.REVISION_REQUESTED:
        collaboration_status = ScopeCollaborationStatus.REVISION_REQUESTED
    elif review_status is ScopeReviewStatus.CUSTOMER_REVIEW:
        collaboration_status = ScopeCollaborationStatus.AWAITING_CONFIRMATION
    elif company_participation is CompanyParticipationStatus.JOINED:
        collaboration_status = ScopeCollaborationStatus.AWAITING_COMPANY_PROPOSAL
    else:
        collaboration_status = ScopeCollaborationStatus.DRAFT

    if current_change is not None:
        current_quote = current_change.quote
    elif proposal is not None:
        current_quote = _quote_from_proposal(proposal)
    else:
        current_quote = None

    approval_rows = (
        await session.execute(
            select(ScopeApproval.role, ScopeApproval.approved_at).where(
                ScopeApproval.scope_version_id == current.id
            )
        )
    ).tuples()
    approval_times = {stored_role: _aware(at) for stored_role, at in approval_rows}
    unique_media_ids = tuple(dict.fromkeys(media_ids))
    return ScopeReviewView(
        job=_job_header(job, viewer, company_participation),
        scope=ScopeReviewScope(
            id=current.id,
            version_label=f"v{current.sequence_number}",
            schema_version=content.schema_version,
            content_hash=current.content_hash,
            locked_at=_aware(current.locked_at) if current.locked_at is not None else None,
            status=review_status,
            item_count=len(content.items),
            work_count=len(included_works),
            exclusion_count=len(exclusions),
            review_required_count=sum(
                item.review_required for group in room_groups for item in group.items
            ),
            room_groups=room_groups,
            location_conditions=content.location_conditions,
            included_works=included_works,
            exclusions=exclusions,
        ),
        proposal_id=agreement_proposal.id if agreement_proposal is not None else None,
        quote=current_quote,
        execution_plan=(
            _execution_plan_from_proposal(agreement_proposal)
            if agreement_proposal is not None
            else None
        ),
        proposal_reason=agreement_proposal.reason if agreement_proposal is not None else None,
        company_participation_status=company_participation,
        collaboration_status=collaboration_status,
        agreement_notice=AGREEMENT_NOTICE,
        approved_changes=approved_changes,
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
