"""A-owned capture submission, task dispatch, and result-import commands."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.ai import AnalysisResult, AnalysisTaskV1
from app.contracts.events import DomainEventType
from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.ports import ProviderError, ProviderErrorKind, TaskQueuePort
from app.contracts.primitives import (
    AnalysisRunId,
    CaptureSessionId,
    TraceId,
    utc_now,
)
from app.modules.analysis.orchestration import request_analysis
from app.modules.analysis_workflow.models import (
    CaptureAnalysisDispatch,
    CaptureAnalysisStatus,
)
from app.modules.analysis_workflow.schemas import (
    CaptureAnalysisResponse,
    capture_analysis_response,
)
from app.modules.capture.models import CaptureSession, MediaAsset
from app.modules.move_job.models import MoveJob, MoveJobStatus
from app.modules.scope.models import ScopeVersion
from app.modules.scope.service import (
    AnalysisDraftInvalidError,
    ScopeResourceNotFoundError,
    ScopeVersionConflictError,
    import_analysis_draft,
)
from app.platform.event_bus import enqueue_domain_event

DEFAULT_BATCH_SIZE = 100
MAX_BATCH_SIZE = 100
DEFAULT_LEASE_SECONDS = 60
DEFAULT_ENQUEUE_TIMEOUT_SECONDS = 10.0
MAX_DISPATCH_RETRY_DELAY_SECONDS = 300


class CaptureAnalysisNotFoundError(LookupError):
    """Raised for a missing, cross-owner, or stale analysis resource."""


class CaptureAnalysisConflictError(ValueError):
    """Raised when capture analysis cannot transition from its current state."""


@dataclass(frozen=True, slots=True)
class ClaimedCaptureAnalysis:
    """One queue task and its A-owned dispatch lease."""

    task: AnalysisTaskV1
    dispatch_token: UUID


@dataclass(frozen=True, slots=True)
class AnalysisDispatchResult:
    """Counts from one bounded analysis dispatch pass."""

    claimed: int
    queued: int
    failed: int


async def submit_capture_analysis(
    session: AsyncSession,
    job_id: UUID,
    capture_session_id: UUID,
    participant_id: UUID,
    *,
    trace_id: str,
    now: datetime | None = None,
) -> CaptureAnalysisResponse:
    """Freeze a capture's READY inventory set and persist one analysis intent."""

    job = await session.scalar(select(MoveJob).where(MoveJob.id == job_id).with_for_update())
    if job is None:
        raise CaptureAnalysisNotFoundError(job_id)

    capture = await session.scalar(
        select(CaptureSession)
        .where(
            CaptureSession.id == capture_session_id,
            CaptureSession.job_id == job_id,
            CaptureSession.created_by_participant_id == participant_id,
        )
        .with_for_update()
    )
    if capture is None:
        raise CaptureAnalysisNotFoundError(capture_session_id)
    if capture.media_consented_at is None:
        raise CaptureAnalysisConflictError(capture_session_id)

    existing = await session.scalar(
        select(CaptureAnalysisDispatch).where(
            CaptureAnalysisDispatch.capture_session_id == capture_session_id
        )
    )
    if existing is not None:
        return capture_analysis_response(existing)
    if job.status in {MoveJobStatus.COMPLETED, MoveJobStatus.CANCELED}:
        raise CaptureAnalysisConflictError(job_id)

    inventory_assets = (
        await session.scalars(
            select(MediaAsset)
            .where(
                MediaAsset.capture_session_id == capture_session_id,
                MediaAsset.media_purpose == MediaPurpose.INVENTORY,
            )
            .order_by(MediaAsset.created_at, MediaAsset.id)
            .with_for_update()
        )
    ).all()
    if not inventory_assets or any(
        asset.status is not MediaAssetStatus.READY for asset in inventory_assets
    ):
        raise CaptureAnalysisConflictError(capture_session_id)

    operation_time = now or utc_now()
    row = CaptureAnalysisDispatch(
        analysis_run_id=uuid4(),
        capture_session_id=capture_session_id,
        move_job_id=job_id,
        submitted_by_participant_id=participant_id,
        trace_id=trace_id,
        scheduled_at=operation_time,
        submitted_at=operation_time,
    )
    session.add(row)
    await session.flush()
    enqueue_domain_event(
        session,
        DomainEventType.CAPTURE_SUBMITTED_V1,
        job_id,
        actor_id=participant_id,
        trace_id=trace_id,
        occurred_at=operation_time,
        payload={
            "capture_session_id": str(capture_session_id),
            "analysis_run_id": str(row.analysis_run_id),
            "inventory_media_asset_ids": [str(asset.id) for asset in inventory_assets],
        },
    )
    return capture_analysis_response(row)


async def get_capture_analysis(
    session: AsyncSession,
    job_id: UUID,
    capture_session_id: UUID,
    participant_id: UUID,
) -> CaptureAnalysisResponse:
    """Return analysis state only to the participant who owns the capture."""

    row = await session.scalar(
        select(CaptureAnalysisDispatch)
        .join(CaptureSession, CaptureSession.id == CaptureAnalysisDispatch.capture_session_id)
        .where(
            CaptureAnalysisDispatch.capture_session_id == capture_session_id,
            CaptureAnalysisDispatch.move_job_id == job_id,
            CaptureSession.created_by_participant_id == participant_id,
        )
    )
    if row is None:
        raise CaptureAnalysisNotFoundError(capture_session_id)
    return capture_analysis_response(row)


async def claim_capture_analyses(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = DEFAULT_BATCH_SIZE,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> tuple[ClaimedCaptureAnalysis, ...]:
    """Lease due analysis intents without blocking another dispatcher."""

    if not 1 <= limit <= MAX_BATCH_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_BATCH_SIZE}")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    claimed_at = now or utc_now()
    rows = (
        await session.scalars(
            select(CaptureAnalysisDispatch)
            .where(
                or_(
                    and_(
                        CaptureAnalysisDispatch.status == CaptureAnalysisStatus.PENDING,
                        CaptureAnalysisDispatch.scheduled_at <= claimed_at,
                    ),
                    and_(
                        CaptureAnalysisDispatch.status == CaptureAnalysisStatus.DISPATCHING,
                        CaptureAnalysisDispatch.dispatch_locked_until <= claimed_at,
                    ),
                )
            )
            .order_by(
                CaptureAnalysisDispatch.scheduled_at,
                CaptureAnalysisDispatch.analysis_run_id,
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    claims: list[ClaimedCaptureAnalysis] = []
    for row in rows:
        if row.status is CaptureAnalysisStatus.PENDING:
            row.dispatch_attempt_count += 1
            row.last_attempt_at = claimed_at
        token = uuid4()
        row.status = CaptureAnalysisStatus.DISPATCHING
        row.dispatch_token = token
        row.dispatch_locked_until = claimed_at + timedelta(seconds=lease_seconds)
        claims.append(
            ClaimedCaptureAnalysis(
                task=AnalysisTaskV1(
                    analysis_run_id=AnalysisRunId(row.analysis_run_id),
                    capture_session_id=CaptureSessionId(row.capture_session_id),
                    attempt_count=1,
                    trace_id=TraceId(row.trace_id),
                ),
                dispatch_token=token,
            )
        )
    await session.flush()
    return tuple(claims)


async def finalize_capture_analysis_dispatch(
    session: AsyncSession,
    claim: ClaimedCaptureAnalysis,
    *,
    provider_task_id: str | None = None,
    error_code: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Apply one queue outcome only while its dispatch lease is current."""

    if (provider_task_id is None) == (error_code is None):
        raise ValueError("dispatch outcome requires exactly one task ID or error code")
    row = await session.scalar(
        select(CaptureAnalysisDispatch)
        .where(CaptureAnalysisDispatch.analysis_run_id == claim.task.analysis_run_id)
        .with_for_update()
    )
    if row is None:
        return False
    if not (
        row.status is CaptureAnalysisStatus.DISPATCHING
        and row.dispatch_token == claim.dispatch_token
    ):
        already_accepted = (
            provider_task_id is not None
            and row.capture_session_id == claim.task.capture_session_id
            and row.trace_id == claim.task.trace_id
            and row.status
            in {
                CaptureAnalysisStatus.QUEUED,
                CaptureAnalysisStatus.RUNNING,
                CaptureAnalysisStatus.COMPLETED,
                CaptureAnalysisStatus.FAILED,
            }
            and row.dispatch_token is None
            and row.provider_task_id in {None, provider_task_id}
        )
        if not already_accepted:
            return False
        row.provider_task_id = provider_task_id
        await session.flush()
        return True

    row.dispatch_token = None
    row.dispatch_locked_until = None
    if error_code is None:
        row.status = CaptureAnalysisStatus.QUEUED
        row.provider_task_id = provider_task_id
        row.last_dispatch_error_code = None
    else:
        operation_time = now or utc_now()
        retry_delay = min(
            2 ** min(row.dispatch_attempt_count, 8),
            MAX_DISPATCH_RETRY_DELAY_SECONDS,
        )
        row.status = CaptureAnalysisStatus.PENDING
        row.scheduled_at = operation_time + timedelta(seconds=retry_delay)
        row.last_dispatch_error_code = error_code
    await session.flush()
    return True


async def dispatch_capture_analyses_once(
    factory: async_sessionmaker[AsyncSession],
    task_queue: TaskQueuePort,
    *,
    queue_name: str,
    handler: str,
    now: datetime | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    enqueue_timeout_seconds: float = DEFAULT_ENQUEUE_TIMEOUT_SECONDS,
) -> AnalysisDispatchResult:
    """Claim and enqueue one bounded batch of durable analysis intents."""

    if enqueue_timeout_seconds <= 0:
        raise ValueError("enqueue_timeout_seconds must be positive")
    if lease_seconds <= enqueue_timeout_seconds:
        raise ValueError("lease_seconds must exceed enqueue_timeout_seconds")
    operation_time = now or utc_now()
    async with factory.begin() as session:
        claims = await claim_capture_analyses(
            session,
            now=operation_time,
            limit=batch_size,
            lease_seconds=lease_seconds,
        )
    if not claims:
        return AnalysisDispatchResult(claimed=0, queued=0, failed=0)

    async def enqueue(claim: ClaimedCaptureAnalysis) -> str | Exception:
        try:
            return await request_analysis(
                task_queue,
                analysis_run_id=claim.task.analysis_run_id,
                capture_session_id=claim.task.capture_session_id,
                trace_id=claim.task.trace_id,
                queue_name=queue_name,
                handler=handler,
                attempt_count=claim.task.attempt_count,
                timeout_seconds=enqueue_timeout_seconds,
            )
        except Exception as error:
            return error

    outcomes = await asyncio.gather(*(enqueue(claim) for claim in claims))
    queued = 0
    failed = 0
    async with factory.begin() as session:
        for claim, outcome in zip(claims, outcomes, strict=True):
            if isinstance(outcome, Exception):
                error_code = (
                    outcome.kind.value if isinstance(outcome, ProviderError) else "unexpected"
                )
                recorded = await finalize_capture_analysis_dispatch(
                    session,
                    claim,
                    error_code=error_code,
                    now=operation_time if now is not None else utc_now(),
                )
                failed += int(recorded)
            else:
                recorded = await finalize_capture_analysis_dispatch(
                    session,
                    claim,
                    provider_task_id=outcome,
                )
                queued += int(recorded)
    return AnalysisDispatchResult(claimed=len(claims), queued=queued, failed=failed)


async def _load_task_row(
    session: AsyncSession,
    task: AnalysisTaskV1,
) -> CaptureAnalysisDispatch:
    row = await session.scalar(
        select(CaptureAnalysisDispatch)
        .where(CaptureAnalysisDispatch.analysis_run_id == task.analysis_run_id)
        .with_for_update()
    )
    if (
        row is None
        or row.capture_session_id != task.capture_session_id
        or row.trace_id != task.trace_id
        or task.attempt_count != 1
    ):
        raise CaptureAnalysisNotFoundError(task.analysis_run_id)
    return row


async def start_capture_analysis(
    session: AsyncSession,
    task: AnalysisTaskV1,
) -> bool:
    """Accept one current task delivery; terminal replays need no provider call."""

    row = await _load_task_row(session, task)
    if row.status in {CaptureAnalysisStatus.COMPLETED, CaptureAnalysisStatus.FAILED}:
        return False
    row.status = CaptureAnalysisStatus.RUNNING
    row.dispatch_token = None
    row.dispatch_locked_until = None
    await session.flush()
    return True


def _mark_failed(
    session: AsyncSession,
    row: CaptureAnalysisDispatch,
    error_kind: ProviderErrorKind,
    retryable: bool,
    completed_at: datetime,
) -> None:
    row.status = CaptureAnalysisStatus.FAILED
    row.failure_code = error_kind.value
    row.retryable = retryable
    row.scope_version_id = None
    row.dispatch_token = None
    row.dispatch_locked_until = None
    row.completed_at = completed_at
    enqueue_domain_event(
        session,
        DomainEventType.ANALYSIS_FAILED_V1,
        row.move_job_id,
        trace_id=row.trace_id,
        occurred_at=completed_at,
        payload={
            "capture_session_id": str(row.capture_session_id),
            "analysis_run_id": str(row.analysis_run_id),
            "error_kind": error_kind.value,
            "retryable": retryable,
        },
    )


async def complete_capture_analysis(
    session: AsyncSession,
    task: AnalysisTaskV1,
    result: AnalysisResult,
    *,
    completed_at: datetime | None = None,
) -> CaptureAnalysisResponse:
    """Import one B result through A's scope command and finalize the workflow."""

    row = await _load_task_row(session, task)
    if result.analysis_run_id != task.analysis_run_id or (
        result.capture_session_id != task.capture_session_id
    ):
        raise CaptureAnalysisConflictError(task.analysis_run_id)
    if row.status is CaptureAnalysisStatus.COMPLETED:
        return capture_analysis_response(row)
    if row.status is not CaptureAnalysisStatus.RUNNING:
        raise CaptureAnalysisConflictError(task.analysis_run_id)

    operation_time = completed_at or utc_now()
    scope_version = await session.scalar(
        select(ScopeVersion).where(
            ScopeVersion.source_analysis_run_id == result.analysis_run_id,
            ScopeVersion.source_capture_session_id == result.capture_session_id,
            ScopeVersion.job_id == row.move_job_id,
        )
    )
    if scope_version is None:
        try:
            async with session.begin_nested():
                imported = await import_analysis_draft(session, row.move_job_id, result)
        except (
            AnalysisDraftInvalidError,
            ScopeResourceNotFoundError,
            ScopeVersionConflictError,
        ):
            _mark_failed(
                session,
                row,
                ProviderErrorKind.INVALID_INPUT,
                False,
                operation_time,
            )
            await session.flush()
            return capture_analysis_response(row)
        scope_version_id = imported.id
    else:
        scope_version_id = scope_version.id

    row.status = CaptureAnalysisStatus.COMPLETED
    row.scope_version_id = scope_version_id
    row.completed_at = operation_time
    enqueue_domain_event(
        session,
        DomainEventType.ANALYSIS_COMPLETED_V1,
        row.move_job_id,
        trace_id=row.trace_id,
        occurred_at=operation_time,
        payload={
            "capture_session_id": str(row.capture_session_id),
            "analysis_run_id": str(row.analysis_run_id),
            "scope_version_id": str(scope_version_id),
        },
    )
    await session.flush()
    return capture_analysis_response(row)


async def fail_capture_analysis(
    session: AsyncSession,
    task: AnalysisTaskV1,
    *,
    error_kind: ProviderErrorKind,
    retryable: bool,
    completed_at: datetime | None = None,
) -> CaptureAnalysisResponse:
    """Finalize one provider-neutral failure exactly once."""

    row = await _load_task_row(session, task)
    if row.status is CaptureAnalysisStatus.FAILED:
        if row.failure_code == error_kind.value and row.retryable is retryable:
            return capture_analysis_response(row)
        raise CaptureAnalysisConflictError(task.analysis_run_id)
    if row.status is CaptureAnalysisStatus.COMPLETED:
        raise CaptureAnalysisConflictError(task.analysis_run_id)
    _mark_failed(
        session,
        row,
        error_kind,
        retryable,
        completed_at or utc_now(),
    )
    await session.flush()
    return capture_analysis_response(row)
