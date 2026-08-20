"""Completion confirmation and append-only audit commands."""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.contracts.actor import ParticipantRole
from app.contracts.events import DomainEventType
from app.contracts.maintenance import BackgroundJobType
from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.model import ContractModel
from app.contracts.ports import StoragePort, validate_storage_url
from app.contracts.primitives import utc_now
from app.modules.background_job.models import BackgroundJob
from app.modules.background_job.service import create_retention_background_job
from app.modules.capture.models import CaptureSession, MediaAsset
from app.modules.completion.models import (
    AuditEvent,
    AuditEventType,
    CompletionConfirmation,
    CompletionEvidence,
    CompletionProblemReport,
    CompletionRequest,
    CompletionRequestStatus,
    CompletionSubmission,
    CompletionSubmissionEvidence,
)
from app.modules.completion.schemas import (
    AuditEventResponse,
    CompletionChecklistItem,
    CompletionChecklistSummary,
    CompletionConfirmationCreate,
    CompletionConfirmationResponse,
    CompletionDecisionCreate,
    CompletionDecisionResponse,
    CompletionDocumentStatus,
    CompletionDocumentSummary,
    CompletionFieldChangeSummary,
    CompletionMediaPreview,
    CompletionProblemReportResponse,
    CompletionRequestCreate,
    CompletionRequestResponse,
    CompletionRequestViewStatus,
    CompletionResult,
    CompletionSubmissionCreate,
    CompletionSubmissionResponse,
    CompletionSummaryView,
    CompletionWorkerShift,
    CompletionWorkerShiftCreate,
)
from app.modules.dispatch.models import DispatchPlan, DispatchSetup, FieldCheckIn
from app.modules.dispatch.schemas import DispatchWorkerOption
from app.modules.field_change.models import ChangeProposalDetail
from app.modules.move_job.models import (
    JobParticipant,
    Location,
    LocationKind,
    MoveJob,
    MoveJobStatus,
)
from app.modules.scope.models import ChangeRequest, ChangeRequestStatus, ScopeVersion
from app.modules.scope_review.models import ScopeProposal, ScopeProposalStatus
from app.modules.scope_review.schemas import QuoteSnapshot, ScopeReviewJobHeader
from app.platform.event_bus import enqueue_domain_event

REQUIRED_COMPLETION_ROLES = (
    ParticipantRole.CUSTOMER,
    ParticipantRole.COMPANY_MANAGER,
)
COMPLETION_REQUEST_TTL = timedelta(days=7)
COMPLETION_READ_URL_TTL_SECONDS = 5 * 60
COMPLETION_CLOCK_SKEW = timedelta(minutes=5)
COMPLETION_MAX_SHIFT_LOOKBACK = timedelta(hours=24)
COMPLETION_STORAGE_TIMEOUT_SECONDS = 10.0


class CompletionResourceNotFoundError(LookupError):
    """Raised for a missing or cross-job completion resource."""


class CompletionConflictError(ValueError):
    """Raised when completion cannot advance from its current state."""


class CompletionInvalidError(ValueError):
    """Raised when completion evidence violates the media policy."""


def add_audit_event(
    session: AsyncSession,
    job_id: UUID,
    event_type: AuditEventType,
    *,
    actor_participant_id: UUID | None = None,
    payload: Mapping[str, Any] | None = None,
) -> AuditEvent:
    """Append one sanitized business fact inside the caller's transaction."""

    event = AuditEvent(
        job_id=job_id,
        event_type=event_type,
        actor_participant_id=actor_participant_id,
        payload=dict(payload or {}),
        occurred_at=utc_now(),
    )
    session.add(event)
    return event


async def _confirmation_response(
    session: AsyncSession,
    confirmation: CompletionConfirmation,
) -> CompletionConfirmationResponse:
    evidence_ids = (
        await session.scalars(
            select(CompletionEvidence.media_asset_id)
            .where(CompletionEvidence.confirmation_id == confirmation.id)
            .order_by(CompletionEvidence.media_asset_id)
        )
    ).all()
    return CompletionConfirmationResponse(
        id=confirmation.id,
        job_id=confirmation.job_id,
        scope_version_id=confirmation.scope_version_id,
        participant_id=confirmation.participant_id,
        role=confirmation.role,
        evidence_media_asset_ids=tuple(evidence_ids),
        confirmed_at=confirmation.confirmed_at,
    )


async def confirm_completion(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
    role: ParticipantRole,
    command: CompletionConfirmationCreate,
    *,
    retention_days: int,
    trace_id: str,
) -> CompletionResult:
    job = await session.scalar(select(MoveJob).where(MoveJob.id == job_id).with_for_update())
    if job is None:
        raise CompletionResourceNotFoundError(job_id)
    if job.status in {MoveJobStatus.COMPLETED, MoveJobStatus.CANCELED}:
        raise CompletionConflictError(job_id)
    participant = await session.scalar(
        select(JobParticipant.id).where(
            JobParticipant.id == participant_id,
            JobParticipant.job_id == job_id,
            JobParticipant.role == role,
            JobParticipant.role.in_(REQUIRED_COMPLETION_ROLES),
        )
    )
    if participant is None:
        raise CompletionResourceNotFoundError(job_id)

    scope_version = await session.scalar(
        select(ScopeVersion).where(
            ScopeVersion.id == command.scope_version_id,
            ScopeVersion.job_id == job_id,
        )
    )
    if scope_version is None:
        raise CompletionResourceNotFoundError(command.scope_version_id)
    if scope_version.locked_at is None:
        raise CompletionConflictError(command.scope_version_id)
    if (
        await session.scalar(
            select(ScopeVersion.id).where(ScopeVersion.parent_version_id == scope_version.id)
        )
        is not None
    ):
        raise CompletionConflictError(command.scope_version_id)
    if (
        await session.scalar(
            select(ChangeRequest.id).where(
                ChangeRequest.job_id == job_id,
                ChangeRequest.status.in_(
                    {
                        ChangeRequestStatus.PENDING,
                        ChangeRequestStatus.CLARIFICATION_REQUESTED,
                    }
                ),
            )
        )
        is not None
    ):
        raise CompletionConflictError(command.scope_version_id)

    existing = await session.scalar(
        select(CompletionConfirmation).where(
            CompletionConfirmation.job_id == job_id,
            CompletionConfirmation.role == role,
        )
    )
    if existing is not None:
        raise CompletionConflictError(job_id)
    other_confirmations = (
        await session.scalars(
            select(CompletionConfirmation).where(CompletionConfirmation.job_id == job_id)
        )
    ).all()
    if any(
        confirmation.scope_version_id != command.scope_version_id
        for confirmation in other_confirmations
    ):
        raise CompletionConflictError(command.scope_version_id)
    if other_confirmations:
        other_evidence_ids = set(
            (
                await session.scalars(
                    select(CompletionEvidence.media_asset_id).where(
                        CompletionEvidence.confirmation_id == other_confirmations[0].id
                    )
                )
            ).all()
        )
        if other_evidence_ids != set(command.evidence_media_asset_ids):
            raise CompletionConflictError(job_id)

    assets = (
        await session.scalars(
            select(MediaAsset)
            .join(CaptureSession, CaptureSession.id == MediaAsset.capture_session_id)
            .join(
                JobParticipant,
                JobParticipant.id == CaptureSession.created_by_participant_id,
            )
            .where(
                MediaAsset.id.in_(command.evidence_media_asset_ids),
                CaptureSession.job_id == job_id,
                JobParticipant.job_id == job_id,
                JobParticipant.role == ParticipantRole.FIELD_WORKER,
                MediaAsset.media_purpose == MediaPurpose.COMPLETION,
                MediaAsset.status.in_({MediaAssetStatus.UPLOADED, MediaAssetStatus.READY}),
            )
        )
    ).all()
    if {asset.id for asset in assets} != set(command.evidence_media_asset_ids) or any(
        not asset.generation or asset.generation != asset.generation.strip() for asset in assets
    ):
        raise CompletionInvalidError(job_id)

    now = utc_now()
    confirmation = CompletionConfirmation(
        job_id=job_id,
        scope_version_id=command.scope_version_id,
        participant_id=participant_id,
        role=role,
        confirmed_at=now,
    )
    session.add(confirmation)
    await session.flush()
    session.add_all(
        CompletionEvidence(
            confirmation_id=confirmation.id,
            media_asset_id=media_asset_id,
        )
        for media_asset_id in command.evidence_media_asset_ids
    )
    add_audit_event(
        session,
        job_id,
        AuditEventType.COMPLETION_CONFIRMED,
        actor_participant_id=participant_id,
        payload={
            "completion_confirmation_id": str(confirmation.id),
            "scope_version_id": str(command.scope_version_id),
            "evidence_media_asset_ids": sorted(
                str(media_asset_id) for media_asset_id in command.evidence_media_asset_ids
            ),
        },
    )
    completed_at = None
    if {stored.role for stored in other_confirmations} | {role} == set(REQUIRED_COMPLETION_ROLES):
        job.status = MoveJobStatus.COMPLETED
        job.completed_at = now
        completed_at = now
        add_audit_event(
            session,
            job_id,
            AuditEventType.JOB_COMPLETED,
            actor_participant_id=participant_id,
            payload={"scope_version_id": str(command.scope_version_id)},
        )
    try:
        if completed_at is not None:
            for asset in assets:
                await create_retention_background_job(
                    session,
                    job_id,
                    asset.id,
                    None,
                    retention_cutoff=now,
                    scheduled_at=now + timedelta(days=retention_days),
                    trace_id=trace_id,
                )
        await session.flush()
    except IntegrityError as error:
        raise CompletionConflictError(job_id) from error
    return CompletionResult(
        confirmation=await _confirmation_response(session, confirmation),
        job_status=job.status,
        completed_at=completed_at,
    )


async def list_completion_confirmations(
    session: AsyncSession,
    job_id: UUID,
) -> tuple[CompletionConfirmationResponse, ...]:
    confirmations = (
        await session.scalars(
            select(CompletionConfirmation)
            .where(CompletionConfirmation.job_id == job_id)
            .order_by(CompletionConfirmation.confirmed_at, CompletionConfirmation.id)
        )
    ).all()
    return tuple(
        [await _confirmation_response(session, confirmation) for confirmation in confirmations]
    )


async def list_audit_events(
    session: AsyncSession,
    job_id: UUID,
) -> tuple[AuditEventResponse, ...]:
    events = (
        await session.scalars(
            select(AuditEvent)
            .where(AuditEvent.job_id == job_id)
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
    ).all()
    return tuple(
        AuditEventResponse(
            id=event.id,
            job_id=event.job_id,
            event_type=event.event_type,
            actor_participant_id=event.actor_participant_id,
            payload=event.payload,
            occurred_at=event.occurred_at,
        )
        for event in events
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _command_hash(command: ContractModel) -> str:
    payload = command.model_dump(mode="json")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _load_completion_job(
    session: AsyncSession,
    job_id: UUID,
    *,
    lock: bool = False,
) -> MoveJob:
    statement = (
        select(MoveJob)
        .where(MoveJob.id == job_id)
        .options(
            selectinload(MoveJob.participants),
            selectinload(MoveJob.locations).selectinload(Location.room_zones),
        )
    )
    if lock:
        statement = statement.with_for_update()
    job = await session.scalar(statement)
    if job is None:
        raise CompletionResourceNotFoundError(job_id)
    return job


def _participant(
    job: MoveJob,
    participant_id: UUID,
    role: ParticipantRole,
) -> JobParticipant:
    participant = next(
        (
            stored
            for stored in job.participants
            if stored.id == participant_id and stored.role is role
        ),
        None,
    )
    if participant is None:
        raise CompletionResourceNotFoundError(job.id)
    return participant


async def _current_locked_scope(
    session: AsyncSession,
    job_id: UUID,
    expected_scope_id: UUID | None = None,
    *,
    lock: bool = False,
) -> ScopeVersion:
    statement = (
        select(ScopeVersion)
        .where(ScopeVersion.job_id == job_id)
        .order_by(ScopeVersion.sequence_number.desc(), ScopeVersion.id.desc())
        .limit(1)
    )
    if lock:
        statement = statement.with_for_update()
    scope = await session.scalar(statement)
    if scope is None:
        raise CompletionResourceNotFoundError(job_id)
    if scope.locked_at is None or (expected_scope_id is not None and scope.id != expected_scope_id):
        raise CompletionConflictError(scope.id)
    if await session.scalar(
        select(ScopeVersion.id).where(ScopeVersion.parent_version_id == scope.id)
    ):
        raise CompletionConflictError(scope.id)
    return scope


def _dispatch_workers(setup: DispatchSetup) -> dict[UUID, DispatchWorkerOption]:
    workers = tuple(
        DispatchWorkerOption.model_validate(item, strict=False) for item in setup.worker_options
    )
    return {worker.id: worker for worker in workers}


async def _submission_evidence_ids(
    session: AsyncSession,
    submission_id: UUID,
) -> tuple[UUID, ...]:
    values = (
        await session.scalars(
            select(CompletionSubmissionEvidence.media_asset_id)
            .where(CompletionSubmissionEvidence.completion_submission_id == submission_id)
            .order_by(CompletionSubmissionEvidence.media_asset_id)
        )
    ).all()
    return tuple(values)


def _shift_response(
    shift: CompletionWorkerShiftCreate,
    worker: DispatchWorkerOption,
) -> CompletionWorkerShift:
    duration = int((shift.ended_at - shift.started_at).total_seconds() // 60)
    return CompletionWorkerShift(
        worker_id=shift.worker_id,
        external_reference=worker.external_reference,
        display_name=worker.display_name,
        role_label=worker.role_label,
        started_at=shift.started_at,
        ended_at=shift.ended_at,
        duration_minutes=duration,
    )


async def _submission_response(
    session: AsyncSession,
    submission: CompletionSubmission,
    setup: DispatchSetup,
) -> CompletionSubmissionResponse:
    workers = _dispatch_workers(setup)
    shifts = tuple(
        CompletionWorkerShiftCreate.model_validate(item, strict=False)
        for item in submission.worker_shifts
    )
    try:
        shift_responses = tuple(
            _shift_response(shift, workers[shift.worker_id]) for shift in shifts
        )
    except KeyError as error:
        raise CompletionConflictError(submission.id) from error
    return CompletionSubmissionResponse(
        completion_submission_id=submission.id,
        client_reference=submission.client_reference,
        job_id=submission.job_id,
        dispatch_id=submission.dispatch_plan_id,
        scope_version_id=submission.scope_version_id,
        submitted_by_participant_id=submission.submitted_by_participant_id,
        completion_media_asset_ids=await _submission_evidence_ids(session, submission.id),
        completed_check_keys=tuple(submission.completed_check_keys),
        worker_shifts=shift_responses,
        onsite_customer_confirmed=submission.onsite_customer_confirmed,
        onsite_confirmed_at=_aware(submission.onsite_confirmed_at),
        work_ended_at=_aware(submission.work_ended_at),
        submitted_at=_aware(submission.submitted_at),
    )


async def _latest_submission(
    session: AsyncSession,
    job_id: UUID,
    *,
    lock: bool = False,
) -> CompletionSubmission | None:
    statement = (
        select(CompletionSubmission)
        .where(CompletionSubmission.job_id == job_id)
        .order_by(CompletionSubmission.submitted_at.desc(), CompletionSubmission.id.desc())
        .limit(1)
    )
    if lock:
        statement = statement.with_for_update()
    return cast(CompletionSubmission | None, await session.scalar(statement))


async def _latest_request(
    session: AsyncSession,
    job_id: UUID,
) -> CompletionRequest | None:
    statement = (
        select(CompletionRequest)
        .where(CompletionRequest.job_id == job_id)
        .order_by(CompletionRequest.requested_at.desc(), CompletionRequest.id.desc())
        .limit(1)
        .with_for_update()
    )
    return cast(CompletionRequest | None, await session.scalar(statement))


async def submit_completion(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
    command: CompletionSubmissionCreate,
    *,
    trace_id: str,
) -> CompletionSubmissionResponse:
    """Persist one exact, checked-in representative-worker completion record."""

    job = await _load_completion_job(session, job_id, lock=True)
    _participant(job, participant_id, ParticipantRole.FIELD_WORKER)
    command_hash = _command_hash(command)
    replay = await session.scalar(
        select(CompletionSubmission)
        .where(
            CompletionSubmission.job_id == job_id,
            CompletionSubmission.client_reference == command.client_reference,
        )
        .with_for_update()
    )
    setup = await session.scalar(
        select(DispatchSetup).where(DispatchSetup.job_id == job_id).with_for_update()
    )
    if setup is None:
        raise CompletionResourceNotFoundError(job_id)
    if replay is not None:
        if (
            replay.command_hash == command_hash
            and replay.submitted_by_participant_id == participant_id
        ):
            return await _submission_response(session, replay, setup)
        raise CompletionConflictError(command.client_reference)
    if job.status in {MoveJobStatus.COMPLETED, MoveJobStatus.CANCELED}:
        raise CompletionConflictError(job_id)

    latest = await _latest_submission(session, job_id, lock=True)
    latest_request = await _latest_request(session, job_id)
    if latest is not None and (
        latest_request is None
        or latest_request.completion_submission_id != latest.id
        or (
            latest_request.status is CompletionRequestStatus.REQUESTED
            and _aware(latest_request.expires_at) > utc_now()
        )
        or latest_request.status is CompletionRequestStatus.CONFIRMED
    ):
        raise CompletionConflictError(latest.id)

    plan = await session.scalar(
        select(DispatchPlan)
        .where(DispatchPlan.id == command.dispatch_id, DispatchPlan.job_id == job_id)
        .with_for_update()
    )
    if plan is None or plan.setup_id != setup.id:
        raise CompletionResourceNotFoundError(command.dispatch_id)
    scope = await _current_locked_scope(
        session,
        job_id,
        command.scope_version_id,
        lock=True,
    )
    if plan.source_scope_version_id != scope.id:
        raise CompletionConflictError(scope.id)
    check_in = await session.scalar(
        select(FieldCheckIn)
        .where(
            FieldCheckIn.job_id == job_id,
            FieldCheckIn.dispatch_plan_id == plan.id,
            FieldCheckIn.participant_id == participant_id,
        )
        .with_for_update()
    )
    if check_in is None:
        raise CompletionConflictError(plan.id)

    required_keys = tuple(str(item["key"]) for item in setup.completion_check_items)
    if set(command.completed_check_keys) != set(required_keys):
        raise CompletionConflictError(setup.id)
    workers = _dispatch_workers(setup)
    selected_worker_ids = tuple(UUID(value) for value in plan.selected_worker_option_ids)
    if set(shift.worker_id for shift in command.worker_shifts) != set(selected_worker_ids):
        raise CompletionConflictError(plan.id)
    lead = workers.get(plan.lead_worker_option_id)
    if lead is None or lead.participant_id != participant_id:
        raise CompletionConflictError(participant_id)

    now = utc_now()
    check_in_at = _aware(check_in.checked_in_at)
    if (
        command.work_ended_at < check_in_at
        or command.onsite_confirmed_at < check_in_at
        or command.work_ended_at > now + COMPLETION_CLOCK_SKEW
        or command.onsite_confirmed_at > now + COMPLETION_CLOCK_SKEW
        or any(
            shift.started_at < check_in_at - COMPLETION_MAX_SHIFT_LOOKBACK
            or shift.ended_at > command.work_ended_at
            for shift in command.worker_shifts
        )
    ):
        raise CompletionConflictError(job_id)

    evidence_ids = set(command.completion_media_asset_ids)
    assets: tuple[MediaAsset, ...] = ()
    if evidence_ids:
        assets = tuple(
            (
                await session.scalars(
                    select(MediaAsset)
                    .join(CaptureSession, CaptureSession.id == MediaAsset.capture_session_id)
                    .where(
                        MediaAsset.id.in_(evidence_ids),
                        CaptureSession.job_id == job_id,
                        CaptureSession.created_by_participant_id == participant_id,
                        MediaAsset.media_purpose == MediaPurpose.COMPLETION,
                        MediaAsset.status.in_({MediaAssetStatus.UPLOADED, MediaAssetStatus.READY}),
                        MediaAsset.generation.is_not(None),
                    )
                )
            ).all()
        )
        if {asset.id for asset in assets} != evidence_ids or any(
            not asset.generation or asset.generation != asset.generation.strip() for asset in assets
        ):
            raise CompletionInvalidError(job_id)

    submission = CompletionSubmission(
        job_id=job_id,
        client_reference=command.client_reference,
        dispatch_plan_id=plan.id,
        scope_version_id=scope.id,
        submitted_by_participant_id=participant_id,
        command_hash=command_hash,
        completed_check_keys=list(required_keys),
        worker_shifts=[shift.model_dump(mode="json") for shift in command.worker_shifts],
        onsite_customer_confirmed=True,
        onsite_confirmed_at=command.onsite_confirmed_at,
        work_ended_at=command.work_ended_at,
        submitted_at=now,
    )
    session.add(submission)
    try:
        await session.flush()
        session.add_all(
            CompletionSubmissionEvidence(
                completion_submission_id=submission.id,
                media_asset_id=asset.id,
            )
            for asset in assets
        )
        enqueue_domain_event(
            session,
            DomainEventType.COMPLETION_SUBMITTED_V1,
            job_id,
            actor_id=participant_id,
            trace_id=trace_id,
            payload={
                "completion_submission_id": str(submission.id),
                "scope_version_id": str(scope.id),
                "field_worker_participant_id": str(participant_id),
            },
        )
        await session.flush()
    except IntegrityError as error:
        raise CompletionConflictError(job_id) from error
    return await _submission_response(session, submission, setup)


async def _problem_response(
    session: AsyncSession,
    request_id: UUID,
) -> CompletionProblemReportResponse | None:
    problem = await session.scalar(
        select(CompletionProblemReport).where(
            CompletionProblemReport.completion_request_id == request_id
        )
    )
    if problem is None:
        return None
    return CompletionProblemReportResponse(
        problem_report_id=problem.id,
        problem_type=problem.problem_type,
        description=problem.description,
        reported_at=_aware(problem.reported_at),
    )


def _request_view_status(
    request: CompletionRequest,
    *,
    now: datetime | None = None,
) -> CompletionRequestViewStatus:
    compared_at = now or utc_now()
    if (
        request.status is CompletionRequestStatus.REQUESTED
        and _aware(request.expires_at) <= compared_at
    ):
        return CompletionRequestViewStatus.EXPIRED
    return CompletionRequestViewStatus(request.status.value)


async def _request_response(
    session: AsyncSession,
    request: CompletionRequest,
    *,
    now: datetime | None = None,
) -> CompletionRequestResponse:
    return CompletionRequestResponse(
        completion_request_id=request.id,
        client_reference=request.client_reference,
        completion_submission_id=request.completion_submission_id,
        status=_request_view_status(request, now=now),
        requested_at=_aware(request.requested_at),
        expires_at=_aware(request.expires_at),
        revoked_at=_aware(request.revoked_at) if request.revoked_at is not None else None,
        decided_at=_aware(request.decided_at) if request.decided_at is not None else None,
        unrecorded_extra_charge=request.unrecorded_extra_charge,
        problem_report=await _problem_response(session, request.id),
        notification_created=True,
    )


async def _ensure_completion_confirmation(
    session: AsyncSession,
    job_id: UUID,
    scope_version_id: UUID,
    participant_id: UUID,
    role: ParticipantRole,
    evidence_ids: tuple[UUID, ...],
    *,
    confirmed_at: datetime,
) -> CompletionConfirmation:
    existing = await session.scalar(
        select(CompletionConfirmation)
        .where(
            CompletionConfirmation.job_id == job_id,
            CompletionConfirmation.role == role,
        )
        .with_for_update()
    )
    if existing is not None:
        stored_evidence = set(
            (
                await session.scalars(
                    select(CompletionEvidence.media_asset_id).where(
                        CompletionEvidence.confirmation_id == existing.id
                    )
                )
            ).all()
        )
        if (
            existing.scope_version_id == scope_version_id
            and existing.participant_id == participant_id
            and stored_evidence == set(evidence_ids)
        ):
            return existing
        raise CompletionConflictError(existing.id)
    confirmation = CompletionConfirmation(
        job_id=job_id,
        scope_version_id=scope_version_id,
        participant_id=participant_id,
        role=role,
        confirmed_at=confirmed_at,
    )
    session.add(confirmation)
    await session.flush()
    session.add_all(
        CompletionEvidence(
            confirmation_id=confirmation.id,
            media_asset_id=media_asset_id,
        )
        for media_asset_id in evidence_ids
    )
    add_audit_event(
        session,
        job_id,
        AuditEventType.COMPLETION_CONFIRMED,
        actor_participant_id=participant_id,
        payload={
            "completion_confirmation_id": str(confirmation.id),
            "scope_version_id": str(scope_version_id),
            "evidence_media_asset_ids": sorted(str(value) for value in evidence_ids),
        },
    )
    return confirmation


async def create_completion_request(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
    command: CompletionRequestCreate,
    *,
    trace_id: str,
) -> CompletionRequestResponse:
    """Record company acceptance and notify the customer of an expiring review."""

    job = await _load_completion_job(session, job_id, lock=True)
    _participant(job, participant_id, ParticipantRole.COMPANY_MANAGER)
    if job.status in {MoveJobStatus.COMPLETED, MoveJobStatus.CANCELED}:
        raise CompletionConflictError(job_id)
    command_hash = _command_hash(command)
    replay = await session.scalar(
        select(CompletionRequest)
        .where(
            CompletionRequest.job_id == job_id,
            CompletionRequest.client_reference == command.client_reference,
        )
        .with_for_update()
    )
    if replay is not None:
        if (
            replay.command_hash == command_hash
            and replay.requested_by_participant_id == participant_id
        ):
            return await _request_response(session, replay)
        raise CompletionConflictError(command.client_reference)

    submission = await session.scalar(
        select(CompletionSubmission)
        .where(
            CompletionSubmission.id == command.completion_submission_id,
            CompletionSubmission.job_id == job_id,
        )
        .with_for_update()
    )
    latest_submission = await _latest_submission(session, job_id, lock=True)
    if submission is None:
        raise CompletionResourceNotFoundError(command.completion_submission_id)
    if latest_submission is None or latest_submission.id != submission.id:
        raise CompletionConflictError(command.completion_submission_id)
    await _current_locked_scope(
        session,
        job_id,
        submission.scope_version_id,
        lock=True,
    )
    now = utc_now()
    latest_request = await _latest_request(session, job_id)
    if latest_request is not None and (
        latest_request.status is CompletionRequestStatus.CONFIRMED
        or (
            latest_request.completion_submission_id == submission.id
            and latest_request.status is CompletionRequestStatus.ISSUE_REPORTED
        )
        or (
            latest_request.status is CompletionRequestStatus.REQUESTED
            and _aware(latest_request.expires_at) > now
        )
    ):
        raise CompletionConflictError(latest_request.id)

    customer = next(
        (stored for stored in job.participants if stored.role is ParticipantRole.CUSTOMER),
        None,
    )
    if customer is None:
        raise CompletionResourceNotFoundError(job_id)
    request = CompletionRequest(
        job_id=job_id,
        client_reference=command.client_reference,
        completion_submission_id=submission.id,
        requested_by_participant_id=participant_id,
        command_hash=command_hash,
        status=CompletionRequestStatus.REQUESTED,
        requested_at=now,
        expires_at=now + COMPLETION_REQUEST_TTL,
    )
    session.add(request)
    try:
        await session.flush()
        enqueue_domain_event(
            session,
            DomainEventType.COMPLETION_REQUESTED_V1,
            job_id,
            actor_id=participant_id,
            trace_id=trace_id,
            payload={
                "completion_request_id": str(request.id),
                "completion_submission_id": str(submission.id),
                "customer_participant_id": str(customer.id),
            },
        )
        await session.flush()
    except IntegrityError as error:
        raise CompletionConflictError(job_id) from error
    return await _request_response(session, request, now=now)


async def revoke_completion_request(
    session: AsyncSession,
    job_id: UUID,
    request_id: UUID,
    participant_id: UUID,
    reason: str,
) -> CompletionRequestResponse:
    """Revoke one still-live customer request without deleting its history."""

    job = await _load_completion_job(session, job_id, lock=True)
    _participant(job, participant_id, ParticipantRole.COMPANY_MANAGER)
    request = await session.scalar(
        select(CompletionRequest)
        .where(CompletionRequest.id == request_id, CompletionRequest.job_id == job_id)
        .with_for_update()
    )
    if request is None:
        raise CompletionResourceNotFoundError(request_id)
    if request.status is CompletionRequestStatus.REVOKED and request.revoke_reason == reason:
        return await _request_response(session, request)
    now = utc_now()
    if request.status is not CompletionRequestStatus.REQUESTED or _aware(request.expires_at) <= now:
        raise CompletionConflictError(request_id)
    request.status = CompletionRequestStatus.REVOKED
    request.revoked_at = now
    request.revoke_reason = reason
    await session.flush()
    return await _request_response(session, request, now=now)


async def _decision_response(
    session: AsyncSession,
    job: MoveJob,
    request: CompletionRequest,
) -> CompletionDecisionResponse:
    problem = await _problem_response(session, request.id)
    if request.decided_at is None:
        raise CompletionConflictError(request.id)
    retention_count = int(
        await session.scalar(
            select(func.count())
            .select_from(BackgroundJob)
            .where(
                BackgroundJob.move_job_id == job.id,
                BackgroundJob.job_type == BackgroundJobType.MEDIA_RETENTION_DELETE,
            )
        )
        or 0
    )
    return CompletionDecisionResponse(
        completion_request_id=request.id,
        decision=(
            "confirm" if request.status is CompletionRequestStatus.CONFIRMED else "report_issue"
        ),
        status=request.status,
        job_status=job.status,
        completed_at=_aware(job.completed_at) if job.completed_at is not None else None,
        decided_at=_aware(request.decided_at),
        problem_report=problem,
        retention_scheduled_count=retention_count,
    )


async def decide_completion_request(
    session: AsyncSession,
    job_id: UUID,
    request_id: UUID,
    participant_id: UUID,
    command: CompletionDecisionCreate,
    *,
    retention_days: int,
    trace_id: str,
) -> CompletionDecisionResponse:
    """Confirm completion or record a separate problem without judging liability."""

    job = await _load_completion_job(session, job_id, lock=True)
    _participant(job, participant_id, ParticipantRole.CUSTOMER)
    request = await session.scalar(
        select(CompletionRequest)
        .where(CompletionRequest.id == request_id, CompletionRequest.job_id == job_id)
        .with_for_update()
    )
    if request is None:
        raise CompletionResourceNotFoundError(request_id)
    decision_hash = _command_hash(command)
    if request.status in {
        CompletionRequestStatus.CONFIRMED,
        CompletionRequestStatus.ISSUE_REPORTED,
    }:
        if request.decision_hash == decision_hash:
            return await _decision_response(session, job, request)
        raise CompletionConflictError(request_id)
    now = utc_now()
    latest = await _latest_request(session, job_id)
    if (
        latest is None
        or latest.id != request.id
        or request.status is not CompletionRequestStatus.REQUESTED
        or _aware(request.expires_at) <= now
        or job.status in {MoveJobStatus.COMPLETED, MoveJobStatus.CANCELED}
    ):
        raise CompletionConflictError(request_id)
    submission = await session.scalar(
        select(CompletionSubmission)
        .where(
            CompletionSubmission.id == request.completion_submission_id,
            CompletionSubmission.job_id == job_id,
        )
        .with_for_update()
    )
    latest_submission = await _latest_submission(session, job_id, lock=True)
    if submission is None:
        raise CompletionResourceNotFoundError(request.completion_submission_id)
    if latest_submission is None or latest_submission.id != submission.id:
        raise CompletionConflictError(submission.id)
    await _current_locked_scope(
        session,
        job_id,
        submission.scope_version_id,
        lock=True,
    )
    evidence_ids = await _submission_evidence_ids(session, submission.id)
    problem: CompletionProblemReport | None = None
    confirmed_assets: tuple[MediaAsset, ...] = ()

    if command.decision == "confirm":
        confirmed_assets = tuple(
            (await session.scalars(select(MediaAsset).where(MediaAsset.id.in_(evidence_ids)))).all()
        )
        if {asset.id for asset in confirmed_assets} != set(evidence_ids) or any(
            asset.status is not MediaAssetStatus.READY
            or not asset.generation
            or asset.generation != asset.generation.strip()
            for asset in confirmed_assets
        ):
            raise CompletionConflictError(submission.id)

    if command.decision == "report_issue":
        assert command.problem_type is not None
        assert command.problem_description is not None
        request.status = CompletionRequestStatus.ISSUE_REPORTED
        request.decision_hash = decision_hash
        request.decided_by_participant_id = participant_id
        request.unrecorded_extra_charge = command.unrecorded_extra_charge
        request.decided_at = now
        problem = CompletionProblemReport(
            job_id=job_id,
            completion_request_id=request.id,
            problem_type=command.problem_type,
            description=command.problem_description,
            reported_by_participant_id=participant_id,
            reported_at=now,
        )
        session.add(problem)
    else:
        manager = _participant(
            job,
            request.requested_by_participant_id,
            ParticipantRole.COMPANY_MANAGER,
        )
        await _ensure_completion_confirmation(
            session,
            job_id,
            submission.scope_version_id,
            manager.id,
            ParticipantRole.COMPANY_MANAGER,
            evidence_ids,
            confirmed_at=now,
        )
        await _ensure_completion_confirmation(
            session,
            job_id,
            submission.scope_version_id,
            participant_id,
            ParticipantRole.CUSTOMER,
            evidence_ids,
            confirmed_at=now,
        )
        request.status = CompletionRequestStatus.CONFIRMED
        request.decision_hash = decision_hash
        request.decided_by_participant_id = participant_id
        request.unrecorded_extra_charge = command.unrecorded_extra_charge
        request.decided_at = now
        job.status = MoveJobStatus.COMPLETED
        job.completed_at = now
        add_audit_event(
            session,
            job_id,
            AuditEventType.JOB_COMPLETED,
            actor_participant_id=participant_id,
            payload={
                "scope_version_id": str(submission.scope_version_id),
                "completion_submission_id": str(submission.id),
                "completion_request_id": str(request.id),
            },
        )
        for asset in confirmed_assets:
            await create_retention_background_job(
                session,
                job_id,
                asset.id,
                None,
                retention_cutoff=now,
                scheduled_at=now + timedelta(days=retention_days),
                trace_id=trace_id,
            )

    try:
        await session.flush()
        enqueue_domain_event(
            session,
            DomainEventType.COMPLETION_DECIDED_V1,
            job_id,
            actor_id=participant_id,
            trace_id=trace_id,
            payload={
                "completion_request_id": str(request.id),
                "completion_submission_id": str(submission.id),
                "decision": command.decision,
                "problem_report_id": str(problem.id) if problem is not None else None,
            },
        )
        await session.flush()
    except IntegrityError as error:
        raise CompletionConflictError(request.id) from error
    return await _decision_response(session, job, request)


def _job_header(job: MoveJob, viewer: JobParticipant) -> ScopeReviewJobHeader:
    names = {participant.role: participant.display_name for participant in job.participants}
    locations = {location.kind: location.label for location in job.locations}
    return ScopeReviewJobHeader(
        job_id=job.id,
        job_code=f"MOVE-{job.id.hex[:8].upper()}",
        title=job.title,
        scheduled_at=_aware(job.scheduled_at) if job.scheduled_at is not None else None,
        customer_display_name=names.get(ParticipantRole.CUSTOMER),
        company_display_name=names.get(ParticipantRole.COMPANY_MANAGER),
        viewer_display_name=viewer.display_name,
        viewer_role=viewer.role,
        origin_summary=locations.get(LocationKind.ORIGIN),
        destination_summary=locations.get(LocationKind.DESTINATION),
    )


async def _quote_for_scope(
    session: AsyncSession,
    job_id: UUID,
    scope_version_id: UUID,
) -> QuoteSnapshot | None:
    proposal = await session.scalar(
        select(ScopeProposal).where(
            ScopeProposal.job_id == job_id,
            ScopeProposal.result_scope_version_id == scope_version_id,
            ScopeProposal.status == ScopeProposalStatus.CONFIRMED,
        )
    )
    if proposal is not None:
        return QuoteSnapshot.model_validate(
            {
                "base_amount_krw": proposal.base_amount_krw,
                "adjustments": proposal.adjustments,
                "total_amount_krw": proposal.total_amount_krw,
            },
            strict=False,
        )
    detail = await session.scalar(
        select(ChangeProposalDetail)
        .join(ChangeRequest, ChangeRequest.id == ChangeProposalDetail.change_request_id)
        .where(
            ChangeRequest.job_id == job_id,
            ChangeRequest.result_scope_version_id == scope_version_id,
            ChangeRequest.status == ChangeRequestStatus.APPROVED,
        )
    )
    if detail is None:
        return None
    return QuoteSnapshot.model_validate(
        {
            "base_amount_krw": detail.base_amount_krw,
            "adjustments": detail.adjustments,
            "total_amount_krw": detail.total_amount_krw,
        },
        strict=False,
    )


async def _field_change_summaries(
    session: AsyncSession,
    job_id: UUID,
) -> tuple[CompletionFieldChangeSummary, ...]:
    rows = (
        (
            await session.execute(
                select(ChangeProposalDetail, ChangeRequest)
                .join(ChangeRequest, ChangeRequest.id == ChangeProposalDetail.change_request_id)
                .where(ChangeRequest.job_id == job_id)
                .order_by(ChangeRequest.created_at, ChangeRequest.id)
            )
        )
        .tuples()
        .all()
    )
    return tuple(
        CompletionFieldChangeSummary(
            proposal_id=request.id,
            title=detail.title,
            status=request.status.value,
            amount_delta_krw=detail.total_amount_krw - detail.base_amount_krw,
            total_amount_krw=detail.total_amount_krw,
            decided_at=(_aware(request.decided_at) if request.decided_at is not None else None),
        )
        for detail, request in rows
    )


async def _completion_media_previews(
    session: AsyncSession,
    storage: StoragePort,
    job: MoveJob,
    submission_id: UUID,
) -> tuple[CompletionMediaPreview, ...]:
    evidence_ids = await _submission_evidence_ids(session, submission_id)
    if not evidence_ids:
        return ()
    assets = tuple(
        (
            await session.scalars(
                select(MediaAsset)
                .where(
                    MediaAsset.id.in_(evidence_ids),
                    MediaAsset.status.in_({MediaAssetStatus.UPLOADED, MediaAssetStatus.READY}),
                    MediaAsset.generation.is_not(None),
                )
                .order_by(MediaAsset.created_at, MediaAsset.id)
            )
        ).all()
    )
    if {asset.id for asset in assets} != set(evidence_ids):
        raise CompletionConflictError(submission_id)
    zone_labels = {zone.id: zone.name for location in job.locations for zone in location.room_zones}
    expires_at = utc_now() + timedelta(seconds=COMPLETION_READ_URL_TTL_SECONDS)
    previews: list[CompletionMediaPreview] = []
    for asset in assets:
        if asset.generation is None or asset.room_zone_id not in zone_labels:
            raise CompletionConflictError(asset.id)
        read_url = await storage.create_read_url(
            object_key=asset.object_key,
            generation=asset.generation,
            expires_in_seconds=COMPLETION_READ_URL_TTL_SECONDS,
            timeout_seconds=COMPLETION_STORAGE_TIMEOUT_SECONDS,
        )
        previews.append(
            CompletionMediaPreview(
                media_asset_id=asset.id,
                room_zone_id=asset.room_zone_id,
                room_zone_label=zone_labels[asset.room_zone_id],
                content_type=asset.content_type,
                read_url=validate_storage_url(
                    read_url,
                    "storage returned an invalid completion read URL",
                ),
                expires_at=expires_at,
            )
        )
    return tuple(previews)


async def get_completion_summary(
    session: AsyncSession,
    storage: StoragePort,
    job_id: UUID,
    participant_id: UUID,
    role: ParticipantRole,
) -> CompletionSummaryView:
    """Compose the provider or customer completion screen from immutable facts."""

    job = await _load_completion_job(session, job_id)
    viewer = _participant(job, participant_id, role)
    if role not in {ParticipantRole.CUSTOMER, ParticipantRole.COMPANY_MANAGER}:
        raise CompletionResourceNotFoundError(job_id)
    scope = await session.scalar(
        select(ScopeVersion)
        .where(ScopeVersion.job_id == job_id, ScopeVersion.locked_at.is_not(None))
        .order_by(ScopeVersion.sequence_number.desc(), ScopeVersion.id.desc())
        .limit(1)
    )
    submission = await _latest_submission(session, job_id)
    request = (
        await session.scalar(
            select(CompletionRequest)
            .where(
                CompletionRequest.job_id == job_id,
                CompletionRequest.completion_submission_id == submission.id,
            )
            .order_by(CompletionRequest.requested_at.desc(), CompletionRequest.id.desc())
            .limit(1)
        )
        if submission is not None
        else None
    )
    if role is ParticipantRole.CUSTOMER and request is None:
        raise CompletionConflictError(job_id)
    setup = await session.scalar(select(DispatchSetup).where(DispatchSetup.job_id == job_id))
    submission_view = (
        await _submission_response(session, submission, setup)
        if submission is not None and setup is not None
        else None
    )
    if submission is not None and setup is None:
        raise CompletionConflictError(submission.id)

    approved_scope = (
        await session.get(ScopeVersion, submission.scope_version_id)
        if submission is not None
        else scope
    )
    if approved_scope is not None and approved_scope.job_id != job_id:
        raise CompletionConflictError(approved_scope.id)
    quote = (
        await _quote_for_scope(session, job_id, approved_scope.id)
        if approved_scope is not None
        else None
    )
    completion_items = tuple(setup.completion_check_items) if setup is not None else ()
    completed_keys = set(submission.completed_check_keys) if submission is not None else set()
    shifts = submission_view.worker_shifts if submission_view is not None else ()
    duration = None
    if shifts:
        started_at = min(shift.started_at for shift in shifts)
        ended_at = max(shift.ended_at for shift in shifts)
        duration = int((ended_at - started_at).total_seconds() // 60)
    documents_ready = submission is not None and quote is not None
    document_status = (
        CompletionDocumentStatus.READY if documents_ready else CompletionDocumentStatus.NOT_READY
    )
    retention_until = await session.scalar(
        select(func.max(BackgroundJob.scheduled_at)).where(
            BackgroundJob.move_job_id == job_id,
            BackgroundJob.job_type == BackgroundJobType.MEDIA_RETENTION_DELETE,
        )
    )
    problem_count = int(
        await session.scalar(
            select(func.count())
            .select_from(CompletionProblemReport)
            .where(CompletionProblemReport.job_id == job_id)
        )
        or 0
    )
    return CompletionSummaryView(
        job=_job_header(job, viewer),
        job_status=job.status,
        completion_submission_id=submission.id if submission is not None else None,
        completed_at=(_aware(submission.work_ended_at) if submission is not None else None),
        final_amount_krw=quote.total_amount_krw if quote is not None else None,
        duration_minutes=duration,
        completion_media=(
            await _completion_media_previews(session, storage, job, submission.id)
            if submission is not None
            else ()
        ),
        completion_media_count=(
            len(await _submission_evidence_ids(session, submission.id))
            if submission is not None
            else 0
        ),
        checklist=CompletionChecklistSummary(
            completed_count=sum(str(item["key"]) in completed_keys for item in completion_items),
            total_count=len(completion_items),
            items=tuple(
                CompletionChecklistItem(
                    key=str(item["key"]),
                    label=str(item["label"]),
                    confirmed=str(item["key"]) in completed_keys,
                )
                for item in completion_items
            ),
        ),
        onsite_confirmation_completed=(
            submission.onsite_customer_confirmed if submission is not None else False
        ),
        worker_shifts=shifts,
        field_changes=await _field_change_summaries(session, job_id),
        quote=quote,
        completion_request=(
            await _request_response(session, request) if request is not None else None
        ),
        approved_scope_version_id=(approved_scope.id if approved_scope is not None else None),
        approved_scope_version_label=(
            f"v{approved_scope.sequence_number}" if approved_scope is not None else None
        ),
        documents=(
            CompletionDocumentSummary(key="quote", name="견적서", status=document_status),
            CompletionDocumentSummary(
                key="changes",
                name="변경 승인 기록",
                status=document_status,
            ),
            CompletionDocumentSummary(
                key="completion",
                name="작업 완료 기록",
                status=document_status,
            ),
            CompletionDocumentSummary(
                key="decision",
                name="완료 확인 기록",
                status=document_status,
            ),
        ),
        archive_ready=documents_ready,
        retention_until=_aware(retention_until) if retention_until is not None else None,
        problem_report_count=problem_count,
    )
