"""Commands and frontend screen view for field issues and change proposals."""

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.contracts.actor import ParticipantRole
from app.contracts.events import DomainEventType
from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.ports import StoragePort
from app.contracts.primitives import utc_now
from app.modules.capture.models import CaptureSession, MediaAsset
from app.modules.capture.service import STORAGE_TIMEOUT_SECONDS
from app.modules.completion.models import AuditEventType, CompletionConfirmation
from app.modules.completion.service import add_audit_event
from app.modules.field_change.models import (
    ChangeProposalDetail,
    FieldIssue,
    FieldIssueEvidence,
)
from app.modules.field_change.schemas import (
    ChangeProposalActor,
    ChangeProposalCreate,
    ChangeProposalDecisionCreate,
    ChangeProposalDecisionResponse,
    ChangeProposalResponse,
    ChangeProposalView,
    FieldIssueCreate,
    FieldIssueEvidenceReadResponse,
    FieldIssueResponse,
    FieldIssueStatus,
)
from app.modules.move_job.models import (
    JobParticipant,
    LocationKind,
    MoveJob,
    MoveJobStatus,
)
from app.modules.scope.models import (
    ChangeRequest,
    ChangeRequestEvidence,
    ChangeRequestStatus,
    ScopeVersion,
)
from app.modules.scope.schemas import ChangeDecisionCreate, ScopeContent
from app.modules.scope.service import (
    ScopeApprovalConflictError,
    ScopeResourceNotFoundError,
    _normalize_scope_content,
    _validate_scope_zones,
    approve_scope_version,
    decide_change_request,
    request_change_clarification,
)
from app.modules.scope_review.models import ScopeProposal, ScopeProposalStatus
from app.modules.scope_review.schemas import (
    QuoteSnapshot,
    ScopeMediaPreview,
    ScopeReviewJobHeader,
)
from app.modules.scope_review.service import _validated_read_url
from app.platform.event_bus import enqueue_domain_event

READ_URL_TTL_SECONDS = 5 * 60
MAX_EVIDENCE_MEDIA = 50


class FieldChangeNotFoundError(LookupError):
    """Raised when a job-scoped field change resource does not exist."""


class FieldChangeConflictError(ValueError):
    """Raised for a stale base, replay mismatch, or invalid lifecycle transition."""


class FieldChangeInvalidError(ValueError):
    """Raised when field evidence or a proposed scope is not usable."""


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def _require_participant(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
    role: ParticipantRole,
) -> JobParticipant:
    participant = await session.scalar(
        select(JobParticipant).where(
            JobParticipant.id == participant_id,
            JobParticipant.job_id == job_id,
            JobParticipant.role == role,
        )
    )
    if participant is None:
        raise FieldChangeNotFoundError(job_id)
    return participant


async def _require_open_job(session: AsyncSession, job_id: UUID) -> MoveJob:
    job = await session.scalar(select(MoveJob).where(MoveJob.id == job_id).with_for_update())
    if job is None:
        raise FieldChangeNotFoundError(job_id)
    if job.status in {MoveJobStatus.COMPLETED, MoveJobStatus.CANCELED}:
        raise FieldChangeConflictError(job_id)
    if (
        await session.scalar(
            select(CompletionConfirmation.id).where(CompletionConfirmation.job_id == job_id)
        )
        is not None
    ):
        raise FieldChangeConflictError(job_id)
    return job


async def _require_current_locked_scope(
    session: AsyncSession,
    job_id: UUID,
    scope_version_id: UUID,
) -> ScopeVersion:
    version = await session.scalar(
        select(ScopeVersion)
        .where(ScopeVersion.id == scope_version_id, ScopeVersion.job_id == job_id)
        .with_for_update()
    )
    if version is None:
        raise FieldChangeNotFoundError(scope_version_id)
    if version.locked_at is None:
        raise FieldChangeConflictError(scope_version_id)
    if (
        await session.scalar(
            select(ScopeVersion.id).where(ScopeVersion.parent_version_id == scope_version_id)
        )
        is not None
    ):
        raise FieldChangeConflictError(scope_version_id)
    return version


async def _issue_evidence_ids(
    session: AsyncSession,
    issue_id: UUID,
) -> tuple[UUID, ...]:
    return tuple(
        (
            await session.scalars(
                select(FieldIssueEvidence.media_asset_id)
                .where(FieldIssueEvidence.field_issue_id == issue_id)
                .order_by(FieldIssueEvidence.media_asset_id)
            )
        ).all()
    )


async def _proposal_for_issue(
    session: AsyncSession,
    issue_id: UUID,
    *,
    lock: bool = False,
) -> tuple[ChangeProposalDetail, ChangeRequest] | None:
    statement = (
        select(ChangeProposalDetail, ChangeRequest)
        .join(
            ChangeRequest,
            ChangeRequest.id == ChangeProposalDetail.change_request_id,
        )
        .where(ChangeProposalDetail.field_issue_id == issue_id)
    )
    if lock:
        statement = statement.with_for_update()
    return (await session.execute(statement)).tuples().one_or_none()


def _issue_status(request: ChangeRequest | None) -> FieldIssueStatus:
    if request is None:
        return FieldIssueStatus.OPEN
    if request.status is ChangeRequestStatus.PENDING:
        return FieldIssueStatus.CUSTOMER_REVIEW
    if request.status is ChangeRequestStatus.CLARIFICATION_REQUESTED:
        return FieldIssueStatus.CLARIFICATION_REQUESTED
    if request.status is ChangeRequestStatus.APPROVED:
        return FieldIssueStatus.APPROVED
    return FieldIssueStatus.REJECTED


async def _field_issue_response(
    session: AsyncSession,
    issue: FieldIssue,
    proposal: tuple[ChangeProposalDetail, ChangeRequest] | None = None,
) -> FieldIssueResponse:
    if proposal is None:
        proposal = await _proposal_for_issue(session, issue.id)
    detail, request = proposal if proposal is not None else (None, None)
    reported_by_role = cast(
        ParticipantRole,
        await session.scalar(
            select(JobParticipant.role).where(
                JobParticipant.id == issue.reported_by_participant_id,
                JobParticipant.job_id == issue.job_id,
            )
        ),
    )
    return FieldIssueResponse(
        field_issue_id=issue.id,
        client_reference=issue.client_reference,
        job_id=issue.job_id,
        base_scope_version_id=issue.base_scope_version_id,
        issue_type=issue.issue_type,
        title=issue.title,
        description=issue.description,
        evidence_media_asset_ids=await _issue_evidence_ids(session, issue.id),
        reported_by_participant_id=issue.reported_by_participant_id,
        reported_by_role=reported_by_role,
        reported_at=_aware(issue.created_at),
        status=_issue_status(request),
        change_proposal_id=detail.change_request_id if detail is not None else None,
    )


def _field_issue_matches(
    issue: FieldIssue,
    evidence_ids: tuple[UUID, ...],
    participant_id: UUID,
    command: FieldIssueCreate,
) -> bool:
    return (
        issue.reported_by_participant_id == participant_id
        and issue.base_scope_version_id == command.base_scope_version_id
        and issue.issue_type is command.issue_type
        and issue.title == command.title
        and issue.description == command.description
        and evidence_ids == tuple(sorted(command.evidence_media_asset_ids))
    )


async def create_field_issue(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
    command: FieldIssueCreate,
) -> FieldIssueResponse:
    """Persist one replay-safe field report with same-actor validated evidence."""

    existing = await session.scalar(
        select(FieldIssue)
        .where(
            FieldIssue.job_id == job_id,
            FieldIssue.client_reference == command.client_reference,
        )
        .with_for_update()
    )
    if existing is not None:
        evidence_ids = await _issue_evidence_ids(session, existing.id)
        if _field_issue_matches(existing, evidence_ids, participant_id, command):
            return await _field_issue_response(session, existing)
        raise FieldChangeConflictError(command.client_reference)

    await _require_open_job(session, job_id)
    participant = await session.scalar(
        select(JobParticipant).where(
            JobParticipant.id == participant_id,
            JobParticipant.job_id == job_id,
            JobParticipant.role.in_(
                {ParticipantRole.COMPANY_MANAGER, ParticipantRole.FIELD_WORKER}
            ),
        )
    )
    if participant is None:
        raise FieldChangeNotFoundError(job_id)
    await _require_current_locked_scope(session, job_id, command.base_scope_version_id)
    evidence = (
        await session.scalars(
            select(MediaAsset)
            .join(CaptureSession, CaptureSession.id == MediaAsset.capture_session_id)
            .where(
                MediaAsset.id.in_(command.evidence_media_asset_ids),
                CaptureSession.job_id == job_id,
                CaptureSession.created_by_participant_id == participant_id,
                MediaAsset.media_purpose == MediaPurpose.CHANGE_EVIDENCE,
                MediaAsset.status.in_({MediaAssetStatus.UPLOADED, MediaAssetStatus.READY}),
            )
        )
    ).all()
    if {asset.id for asset in evidence} != set(command.evidence_media_asset_ids):
        raise FieldChangeInvalidError(job_id)

    issue = FieldIssue(
        job_id=job_id,
        client_reference=command.client_reference,
        base_scope_version_id=command.base_scope_version_id,
        reported_by_participant_id=participant_id,
        issue_type=command.issue_type,
        title=command.title,
        description=command.description,
        created_at=utc_now(),
    )
    session.add(issue)
    try:
        await session.flush()
        session.add_all(
            FieldIssueEvidence(field_issue_id=issue.id, media_asset_id=media_asset_id)
            for media_asset_id in command.evidence_media_asset_ids
        )
        await session.flush()
    except IntegrityError as error:
        raise FieldChangeConflictError(command.client_reference) from error
    return await _field_issue_response(session, issue)


async def list_field_issues(
    session: AsyncSession,
    job_id: UUID,
) -> tuple[FieldIssueResponse, ...]:
    issues = (
        await session.scalars(
            select(FieldIssue)
            .where(FieldIssue.job_id == job_id)
            .order_by(FieldIssue.created_at, FieldIssue.id)
        )
    ).all()
    return tuple([await _field_issue_response(session, issue) for issue in issues])


async def create_field_issue_evidence_read_url(
    session: AsyncSession,
    storage: StoragePort,
    job_id: UUID,
    field_issue_id: UUID,
    media_asset_id: UUID,
    participant_id: UUID,
    role: ParticipantRole,
) -> FieldIssueEvidenceReadResponse:
    """Issue a private preview only to an authorized company or field participant."""

    if role not in {ParticipantRole.COMPANY_MANAGER, ParticipantRole.FIELD_WORKER}:
        raise FieldChangeNotFoundError(field_issue_id)
    await _require_participant(session, job_id, participant_id, role)
    asset = await session.scalar(
        select(MediaAsset)
        .join(
            FieldIssueEvidence,
            FieldIssueEvidence.media_asset_id == MediaAsset.id,
        )
        .join(FieldIssue, FieldIssue.id == FieldIssueEvidence.field_issue_id)
        .join(CaptureSession, CaptureSession.id == MediaAsset.capture_session_id)
        .where(
            FieldIssue.id == field_issue_id,
            FieldIssue.job_id == job_id,
            MediaAsset.id == media_asset_id,
            CaptureSession.job_id == job_id,
            MediaAsset.media_purpose == MediaPurpose.CHANGE_EVIDENCE,
        )
    )
    if asset is None:
        raise FieldChangeNotFoundError(media_asset_id)
    if (
        asset.status is not MediaAssetStatus.READY
        or not asset.generation
        or asset.generation != asset.generation.strip()
    ):
        raise FieldChangeConflictError(media_asset_id)

    expires_at = utc_now() + timedelta(seconds=READ_URL_TTL_SECONDS)
    read_url = await storage.create_read_url(
        object_key=asset.object_key,
        generation=asset.generation,
        expires_in_seconds=READ_URL_TTL_SECONDS,
        timeout_seconds=STORAGE_TIMEOUT_SECONDS,
    )
    return FieldIssueEvidenceReadResponse(
        media_asset_id=asset.id,
        room_zone_id=asset.room_zone_id,
        content_type=asset.content_type,
        read_url=_validated_read_url(read_url),
        expires_at=expires_at,
    )


async def _effective_total_amount(
    session: AsyncSession,
    job_id: UUID,
    scope_version_id: UUID,
) -> int:
    initial_total = await session.scalar(
        select(ScopeProposal.total_amount_krw).where(
            ScopeProposal.job_id == job_id,
            ScopeProposal.result_scope_version_id == scope_version_id,
            ScopeProposal.status == ScopeProposalStatus.CONFIRMED,
        )
    )
    if initial_total is not None:
        return initial_total
    changed_total = await session.scalar(
        select(ChangeProposalDetail.total_amount_krw)
        .join(
            ChangeRequest,
            ChangeRequest.id == ChangeProposalDetail.change_request_id,
        )
        .where(
            ChangeRequest.job_id == job_id,
            ChangeRequest.result_scope_version_id == scope_version_id,
            ChangeRequest.status == ChangeRequestStatus.APPROVED,
        )
    )
    if changed_total is None:
        raise FieldChangeConflictError(scope_version_id)
    return changed_total


def _quote_from_detail(detail: ChangeProposalDetail) -> QuoteSnapshot:
    return QuoteSnapshot.model_validate(
        {
            "base_amount_krw": detail.base_amount_krw,
            "adjustments": detail.adjustments,
            "total_amount_krw": detail.total_amount_krw,
        },
        strict=False,
    )


def _proposal_response(
    detail: ChangeProposalDetail,
    request: ChangeRequest,
) -> ChangeProposalResponse:
    return ChangeProposalResponse(
        proposal_id=request.id,
        field_issue_id=detail.field_issue_id,
        status=request.status,
        base_scope_version_id=request.base_scope_version_id,
        result_scope_version_id=request.result_scope_version_id,
        quote=_quote_from_detail(detail),
        requested_at=_aware(request.created_at),
    )


def _proposal_matches(
    detail: ChangeProposalDetail,
    request: ChangeRequest,
    participant_id: UUID,
    command: ChangeProposalCreate,
) -> bool:
    return (
        request.requested_by_participant_id == participant_id
        and request.base_scope_version_id == command.base_scope_version_id
        and request.description == command.reason
        and ScopeContent.model_validate(request.proposed_content, strict=False)
        == _normalize_scope_content(command.proposed_content)
        and detail.title == command.title
        and _quote_from_detail(detail) == command.quote
    )


async def create_change_proposal(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
    command: ChangeProposalCreate,
    *,
    trace_id: str,
) -> ChangeProposalResponse:
    """Convert one field issue into one immutable, quoted customer proposal."""

    issue = await session.scalar(
        select(FieldIssue)
        .where(FieldIssue.id == command.field_issue_id, FieldIssue.job_id == job_id)
        .with_for_update()
    )
    if issue is None:
        raise FieldChangeNotFoundError(command.field_issue_id)
    existing = await _proposal_for_issue(session, issue.id, lock=True)
    if existing is not None:
        detail, request = existing
        if _proposal_matches(detail, request, participant_id, command):
            return _proposal_response(detail, request)
        raise FieldChangeConflictError(issue.id)

    await _require_open_job(session, job_id)
    await _require_participant(
        session,
        job_id,
        participant_id,
        ParticipantRole.COMPANY_MANAGER,
    )
    if issue.base_scope_version_id != command.base_scope_version_id:
        raise FieldChangeConflictError(command.base_scope_version_id)
    base = await _require_current_locked_scope(
        session,
        job_id,
        command.base_scope_version_id,
    )
    expected_base_amount = await _effective_total_amount(session, job_id, base.id)
    if command.quote.base_amount_krw != expected_base_amount:
        raise FieldChangeConflictError(base.id)

    try:
        await _validate_scope_zones(session, job_id, command.proposed_content)
    except ScopeResourceNotFoundError as error:
        raise FieldChangeNotFoundError(job_id) from error
    normalized_content = _normalize_scope_content(command.proposed_content)
    if normalized_content.model_dump(mode="json") == base.content:
        raise FieldChangeInvalidError(base.id)

    evidence_ids = await _issue_evidence_ids(session, issue.id)
    ready_evidence = (
        await session.scalars(
            select(MediaAsset)
            .join(CaptureSession, CaptureSession.id == MediaAsset.capture_session_id)
            .where(
                MediaAsset.id.in_(evidence_ids),
                CaptureSession.job_id == job_id,
                MediaAsset.media_purpose == MediaPurpose.CHANGE_EVIDENCE,
                MediaAsset.status.in_({MediaAssetStatus.UPLOADED, MediaAssetStatus.READY}),
                MediaAsset.generation.is_not(None),
            )
        )
    ).all()
    if not evidence_ids or {asset.id for asset in ready_evidence} != set(evidence_ids):
        raise FieldChangeInvalidError(issue.id)

    request = ChangeRequest(
        job_id=job_id,
        base_scope_version_id=base.id,
        requested_by_participant_id=participant_id,
        description=command.reason,
        proposed_content=normalized_content.model_dump(mode="json"),
        status=ChangeRequestStatus.PENDING,
        created_at=utc_now(),
    )
    session.add(request)
    try:
        await session.flush()
        detail = ChangeProposalDetail(
            change_request_id=request.id,
            field_issue_id=issue.id,
            title=command.title,
            base_amount_krw=command.quote.base_amount_krw,
            adjustments=[
                adjustment.model_dump(mode="json") for adjustment in command.quote.adjustments
            ],
            total_amount_krw=command.quote.total_amount_krw,
            created_at=request.created_at,
        )
        session.add(detail)
        session.add_all(
            ChangeRequestEvidence(
                change_request_id=request.id,
                media_asset_id=media_asset_id,
            )
            for media_asset_id in evidence_ids
        )
        add_audit_event(
            session,
            job_id,
            AuditEventType.CHANGE_REQUESTED,
            actor_participant_id=participant_id,
            payload={
                "change_request_id": str(request.id),
                "field_issue_id": str(issue.id),
                "base_scope_version_id": str(base.id),
                "evidence_media_asset_ids": sorted(str(value) for value in evidence_ids),
            },
        )
        event_evidence: list[JsonValue] = [str(value) for value in sorted(evidence_ids)]
        enqueue_domain_event(
            session,
            DomainEventType.CHANGE_REQUESTED_V1,
            job_id,
            actor_id=participant_id,
            trace_id=trace_id,
            payload={
                "change_request_id": str(request.id),
                "base_scope_version_id": str(base.id),
                "evidence_media_asset_ids": event_evidence,
            },
        )
        await session.flush()
    except IntegrityError as error:
        raise FieldChangeConflictError(issue.id) from error
    return _proposal_response(detail, request)


async def _load_proposal(
    session: AsyncSession,
    job_id: UUID,
    proposal_id: UUID,
    *,
    lock: bool = False,
) -> tuple[ChangeProposalDetail, ChangeRequest, FieldIssue]:
    statement = (
        select(ChangeProposalDetail, ChangeRequest, FieldIssue)
        .join(ChangeRequest, ChangeRequest.id == ChangeProposalDetail.change_request_id)
        .join(FieldIssue, FieldIssue.id == ChangeProposalDetail.field_issue_id)
        .where(
            ChangeRequest.id == proposal_id,
            ChangeRequest.job_id == job_id,
            FieldIssue.job_id == job_id,
        )
    )
    if lock:
        statement = statement.with_for_update()
    result = (await session.execute(statement)).tuples().one_or_none()
    if result is None:
        raise FieldChangeNotFoundError(proposal_id)
    return result


async def _evidence_previews(
    session: AsyncSession,
    storage: StoragePort,
    job_id: UUID,
    issue_id: UUID,
) -> tuple[ScopeMediaPreview, ...]:
    evidence_ids = await _issue_evidence_ids(session, issue_id)
    assets = (
        await session.scalars(
            select(MediaAsset)
            .join(CaptureSession, CaptureSession.id == MediaAsset.capture_session_id)
            .where(
                MediaAsset.id.in_(evidence_ids[:MAX_EVIDENCE_MEDIA]),
                CaptureSession.job_id == job_id,
                MediaAsset.status.in_({MediaAssetStatus.UPLOADED, MediaAssetStatus.READY}),
                MediaAsset.generation.is_not(None),
            )
            .order_by(MediaAsset.created_at, MediaAsset.id)
        )
    ).all()
    if len(assets) != len(evidence_ids):
        raise FieldChangeConflictError(issue_id)
    expires_at = utc_now() + timedelta(seconds=READ_URL_TTL_SECONDS)
    previews: list[ScopeMediaPreview] = []
    for asset in assets:
        if asset.generation is None or asset.generation != asset.generation.strip():
            raise FieldChangeConflictError(asset.id)
        read_url = await storage.create_read_url(
            object_key=asset.object_key,
            generation=asset.generation,
            expires_in_seconds=READ_URL_TTL_SECONDS,
            timeout_seconds=STORAGE_TIMEOUT_SECONDS,
        )
        validated_url = _validated_read_url(read_url)
        previews.append(
            ScopeMediaPreview(
                media_asset_id=asset.id,
                room_zone_id=asset.room_zone_id,
                content_type=asset.content_type,
                read_url=validated_url,
                expires_at=expires_at,
            )
        )
    return tuple(previews)


def _actor(participant: JobParticipant | None) -> ChangeProposalActor | None:
    if participant is None:
        return None
    return ChangeProposalActor(
        participant_id=participant.id,
        display_name=participant.display_name,
        role=participant.role,
    )


async def get_change_proposal(
    session: AsyncSession,
    storage: StoragePort,
    job_id: UUID,
    proposal_id: UUID,
    participant_id: UUID,
    role: ParticipantRole,
) -> ChangeProposalView:
    """Compose a privacy-safe, signed-media customer decision screen."""

    detail, request, issue = await _load_proposal(session, job_id, proposal_id)
    job = await session.scalar(
        select(MoveJob)
        .where(MoveJob.id == job_id)
        .options(
            selectinload(MoveJob.participants),
            selectinload(MoveJob.locations),
        )
    )
    if job is None:
        raise FieldChangeNotFoundError(job_id)
    viewer = next(
        (
            stored
            for stored in job.participants
            if stored.id == participant_id and stored.role is role
        ),
        None,
    )
    if viewer is None:
        raise FieldChangeNotFoundError(job_id)
    base = await session.get(ScopeVersion, request.base_scope_version_id)
    if base is None or base.job_id != job_id:
        raise FieldChangeConflictError(request.base_scope_version_id)
    requested_by = next(
        (stored for stored in job.participants if stored.id == request.requested_by_participant_id),
        None,
    )
    decided_by = next(
        (stored for stored in job.participants if stored.id == request.decided_by_participant_id),
        None,
    )
    if requested_by is None:
        raise FieldChangeConflictError(request.id)
    requested_by_actor = _actor(requested_by)
    assert requested_by_actor is not None
    participant_names = {stored.role: stored.display_name for stored in job.participants}
    location_names = {location.kind: location.label for location in job.locations}
    return ChangeProposalView(
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
        proposal_id=request.id,
        field_issue_id=issue.id,
        status=request.status,
        title=detail.title,
        reason=request.description,
        base_scope_version_id=request.base_scope_version_id,
        base_scope_version_label=f"v{base.sequence_number}",
        result_scope_version_id=request.result_scope_version_id,
        evidence_media=await _evidence_previews(session, storage, job_id, issue.id),
        quote=_quote_from_detail(detail),
        requested_by=requested_by_actor,
        requested_at=_aware(request.created_at),
        clarification_note=request.clarification_request,
        clarification_requested_at=(
            _aware(request.clarification_requested_at)
            if request.clarification_requested_at is not None
            else None
        ),
        explanation=request.explanation,
        explained_at=_aware(request.explained_at) if request.explained_at is not None else None,
        decided_by=_actor(decided_by),
        decided_at=_aware(request.decided_at) if request.decided_at is not None else None,
        decision_note=request.decision_note,
    )


def _decision_response(request: ChangeRequest) -> ChangeProposalDecisionResponse:
    return ChangeProposalDecisionResponse(
        proposal_id=request.id,
        status=request.status,
        result_scope_version_id=request.result_scope_version_id,
        clarification_requested_at=(
            _aware(request.clarification_requested_at)
            if request.clarification_requested_at is not None
            else None
        ),
        decided_at=_aware(request.decided_at) if request.decided_at is not None else None,
    )


def _matches_decision_replay(
    request: ChangeRequest,
    command: ChangeProposalDecisionCreate,
) -> bool:
    if command.decision == "approve":
        return (
            request.status is ChangeRequestStatus.APPROVED and request.decision_note == command.note
        )
    if command.decision == "reject":
        return (
            request.status is ChangeRequestStatus.REJECTED and request.decision_note == command.note
        )
    return (
        request.status is ChangeRequestStatus.CLARIFICATION_REQUESTED
        and request.clarification_request == command.note
    )


async def decide_change_proposal(
    session: AsyncSession,
    job_id: UUID,
    proposal_id: UUID,
    participant_id: UUID,
    command: ChangeProposalDecisionCreate,
    *,
    trace_id: str,
) -> ChangeProposalDecisionResponse:
    """Record the customer's exact decision and lock an approved result scope."""

    await _require_participant(session, job_id, participant_id, ParticipantRole.CUSTOMER)
    detail, request, _ = await _load_proposal(session, job_id, proposal_id, lock=True)
    if request.status is not ChangeRequestStatus.PENDING:
        if _matches_decision_replay(request, command):
            return _decision_response(request)
        raise FieldChangeConflictError(proposal_id)

    if command.decision == "request_clarification":
        assert command.note is not None
        updated = await request_change_clarification(
            session,
            job_id,
            proposal_id,
            participant_id,
            command.note,
        )
        request.status = updated.status
        request.clarification_requested_by_participant_id = (
            updated.clarification_requested_by_participant_id
        )
        request.clarification_request = updated.clarification_request
        request.clarification_requested_at = updated.clarification_requested_at
        return _decision_response(request)

    if command.decision == "approve":
        evidence_statuses = tuple(
            (
                await session.scalars(
                    select(MediaAsset.status)
                    .join(
                        FieldIssueEvidence,
                        FieldIssueEvidence.media_asset_id == MediaAsset.id,
                    )
                    .where(FieldIssueEvidence.field_issue_id == detail.field_issue_id)
                )
            ).all()
        )
        if not evidence_statuses or any(
            evidence_status is not MediaAssetStatus.READY for evidence_status in evidence_statuses
        ):
            raise FieldChangeConflictError(proposal_id)

    try:
        updated = await decide_change_request(
            session,
            job_id,
            proposal_id,
            participant_id,
            ChangeDecisionCreate(
                decision=command.decision,
                note=command.note,
            ),
        )
    except (ScopeResourceNotFoundError, ScopeApprovalConflictError) as error:
        raise FieldChangeConflictError(proposal_id) from error
    request.status = updated.status
    request.result_scope_version_id = updated.result_scope_version_id
    request.decision_note = updated.decision_note
    request.decided_by_participant_id = updated.decided_by_participant_id
    request.decided_at = updated.decided_at
    if command.decision == "approve":
        if request.result_scope_version_id is None:
            raise FieldChangeConflictError(proposal_id)
        await approve_scope_version(
            session,
            job_id,
            request.result_scope_version_id,
            request.requested_by_participant_id,
            ParticipantRole.COMPANY_MANAGER,
        )
        await approve_scope_version(
            session,
            job_id,
            request.result_scope_version_id,
            participant_id,
            ParticipantRole.CUSTOMER,
            trace_id=trace_id,
        )
    await session.flush()
    return _decision_response(request)


async def explain_change_proposal(
    session: AsyncSession,
    job_id: UUID,
    proposal_id: UUID,
    participant_id: UUID,
    explanation: str,
) -> ChangeProposalResponse:
    """Let the proposing company answer one customer clarification request."""

    await _require_participant(
        session,
        job_id,
        participant_id,
        ParticipantRole.COMPANY_MANAGER,
    )
    detail, request, _ = await _load_proposal(session, job_id, proposal_id, lock=True)
    if request.status is ChangeRequestStatus.PENDING and request.explanation == explanation:
        return _proposal_response(detail, request)
    if (
        request.status is not ChangeRequestStatus.CLARIFICATION_REQUESTED
        or request.requested_by_participant_id != participant_id
    ):
        raise FieldChangeConflictError(proposal_id)
    request.status = ChangeRequestStatus.PENDING
    request.explanation = explanation
    request.explained_at = utc_now()
    add_audit_event(
        session,
        job_id,
        AuditEventType.CHANGE_EXPLAINED,
        actor_participant_id=participant_id,
        payload={"change_request_id": str(proposal_id)},
    )
    await session.flush()
    return _proposal_response(detail, request)
