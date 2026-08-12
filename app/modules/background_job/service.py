"""Media-retention job creation, dispatch, retry, and result commands."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.events import DomainEventType
from app.contracts.maintenance import (
    BackgroundJobType,
    MediaDeletionOutcome,
    MediaDeletionResultV1,
    MediaDeletionTaskV1,
    MediaDeletionWorkV1,
)
from app.contracts.media import MediaAssetStatus
from app.contracts.ports import ProviderError, TaskQueuePort
from app.contracts.primitives import (
    BackgroundJobId,
    IdempotencyKey,
    JobId,
    MediaAssetId,
    utc_now,
)
from app.modules.background_job.models import BackgroundJob, BackgroundJobStatus
from app.modules.background_job.schemas import BackgroundJobResponse
from app.modules.capture.models import CaptureSession, MediaAsset
from app.modules.move_job.models import MoveJob, MoveJobStatus
from app.platform.event_bus import enqueue_domain_event

DEFAULT_BATCH_SIZE = 100
MAX_BATCH_SIZE = 100
DEFAULT_LEASE_SECONDS = 60
DEFAULT_ENQUEUE_TIMEOUT_SECONDS = 10.0
DEFAULT_EXECUTION_TIMEOUT_SECONDS = 15 * 60
DELETABLE_MEDIA_STATUSES = frozenset(
    {
        MediaAssetStatus.UPLOADED,
        MediaAssetStatus.READY,
        MediaAssetStatus.FAILED,
    }
)


class BackgroundJobNotFoundError(LookupError):
    """Raised for a missing or cross-job maintenance resource."""


class BackgroundJobConflictError(ValueError):
    """Raised when a maintenance operation violates policy or state."""


@dataclass(frozen=True, slots=True)
class ClaimedBackgroundJob:
    """One task and lease token carried outside the claim transaction."""

    task: MediaDeletionTaskV1
    dispatch_token: UUID


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Counts from one bounded task dispatch pass."""

    claimed: int
    queued: int
    failed: int


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _response(row: BackgroundJob) -> BackgroundJobResponse:
    return BackgroundJobResponse(
        id=row.id,
        job_id=row.move_job_id,
        media_asset_id=row.media_asset_id,
        job_type=row.job_type,
        status=row.status,
        scheduled_at=_aware(row.scheduled_at),
        attempt_count=row.attempt_count,
        last_error_code=row.last_error_code,
        created_at=_aware(row.created_at),
        last_attempt_at=_aware(row.last_attempt_at) if row.last_attempt_at is not None else None,
        execution_deadline_at=(
            _aware(row.execution_deadline_at) if row.execution_deadline_at is not None else None
        ),
        completed_at=_aware(row.completed_at) if row.completed_at is not None else None,
    )


async def create_retention_background_job(
    session: AsyncSession,
    job_id: UUID,
    media_asset_id: UUID,
    participant_id: UUID,
    *,
    retention_cutoff: datetime,
    trace_id: str,
    now: datetime | None = None,
) -> BackgroundJobResponse:
    """Persist one immutable deletion target after server-side retention checks."""

    if retention_cutoff.tzinfo is None or retention_cutoff.utcoffset() is None:
        raise ValueError("retention_cutoff must include a timezone")
    target = (
        await session.execute(
            select(MediaAsset, MoveJob)
            .join(CaptureSession, CaptureSession.id == MediaAsset.capture_session_id)
            .join(MoveJob, MoveJob.id == CaptureSession.job_id)
            .where(MediaAsset.id == media_asset_id, MoveJob.id == job_id)
        )
    ).one_or_none()
    if target is None:
        raise BackgroundJobNotFoundError(media_asset_id)
    asset, move_job = target

    existing = await session.scalar(
        select(BackgroundJob).where(
            BackgroundJob.job_type == BackgroundJobType.MEDIA_RETENTION_DELETE,
            BackgroundJob.media_asset_id == media_asset_id,
        )
    )
    if existing is not None:
        return _response(existing)
    if (
        move_job.status is not MoveJobStatus.COMPLETED
        or move_job.completed_at is None
        or _aware(move_job.completed_at) > retention_cutoff
        or asset.status not in DELETABLE_MEDIA_STATUSES
        or not asset.generation
    ):
        raise BackgroundJobConflictError(media_asset_id)

    operation_time = now or utc_now()
    row = BackgroundJob(
        move_job_id=job_id,
        media_asset_id=media_asset_id,
        job_type=BackgroundJobType.MEDIA_RETENTION_DELETE,
        target_object_key=asset.object_key,
        target_generation=asset.generation,
        trace_id=trace_id,
        scheduled_at=operation_time,
        created_by_participant_id=participant_id,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        concurrent = await session.scalar(
            select(BackgroundJob).where(
                BackgroundJob.job_type == BackgroundJobType.MEDIA_RETENTION_DELETE,
                BackgroundJob.media_asset_id == media_asset_id,
            )
        )
        if concurrent is None:
            raise
        return _response(concurrent)

    await session.flush()
    return _response(row)


async def list_background_jobs(
    session: AsyncSession,
    job_id: UUID,
) -> tuple[BackgroundJobResponse, ...]:
    rows = (
        await session.scalars(
            select(BackgroundJob)
            .where(BackgroundJob.move_job_id == job_id)
            .order_by(BackgroundJob.created_at, BackgroundJob.id)
        )
    ).all()
    return tuple(_response(row) for row in rows)


async def retry_background_job(
    session: AsyncSession,
    job_id: UUID,
    background_job_id: UUID,
    *,
    now: datetime | None = None,
) -> BackgroundJobResponse:
    row = await session.scalar(
        select(BackgroundJob)
        .where(
            BackgroundJob.id == background_job_id,
            BackgroundJob.move_job_id == job_id,
        )
        .with_for_update()
    )
    if row is None:
        raise BackgroundJobNotFoundError(background_job_id)
    operation_time = now or utc_now()
    execution_expired = (
        row.status is BackgroundJobStatus.RUNNING
        and row.execution_deadline_at is not None
        and _aware(row.execution_deadline_at) <= operation_time
    )
    if row.status is not BackgroundJobStatus.FAILED and not execution_expired:
        raise BackgroundJobConflictError(background_job_id)

    row.status = BackgroundJobStatus.PENDING
    row.scheduled_at = operation_time
    row.provider_task_id = None
    row.last_error_code = None
    row.execution_deadline_at = None
    row.completed_at = None
    await session.flush()
    return _response(row)


async def claim_background_jobs(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = DEFAULT_BATCH_SIZE,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> tuple[ClaimedBackgroundJob, ...]:
    if not 1 <= limit <= MAX_BATCH_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_BATCH_SIZE}")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    claimed_at = now or utc_now()
    rows = (
        await session.scalars(
            select(BackgroundJob)
            .where(
                or_(
                    and_(
                        BackgroundJob.status == BackgroundJobStatus.PENDING,
                        BackgroundJob.scheduled_at <= claimed_at,
                    ),
                    and_(
                        BackgroundJob.status == BackgroundJobStatus.DISPATCHING,
                        BackgroundJob.dispatch_locked_until <= claimed_at,
                    ),
                )
            )
            .order_by(BackgroundJob.scheduled_at, BackgroundJob.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    claims: list[ClaimedBackgroundJob] = []
    for row in rows:
        if row.status is BackgroundJobStatus.PENDING:
            row.attempt_count += 1
            row.last_attempt_at = claimed_at
        token = uuid4()
        row.status = BackgroundJobStatus.DISPATCHING
        row.dispatch_token = token
        row.dispatch_locked_until = claimed_at + timedelta(seconds=lease_seconds)
        claims.append(
            ClaimedBackgroundJob(
                task=MediaDeletionTaskV1(
                    background_job_id=BackgroundJobId(row.id),
                    attempt_count=row.attempt_count,
                    trace_id=row.trace_id,
                ),
                dispatch_token=token,
            )
        )
    await session.flush()
    return tuple(claims)


async def finalize_background_job_dispatch(
    session: AsyncSession,
    claim: ClaimedBackgroundJob,
    *,
    provider_task_id: str | None = None,
    error_code: str | None = None,
    completed_at: datetime | None = None,
) -> bool:
    if (provider_task_id is None) == (error_code is None):
        raise ValueError("dispatch outcome requires exactly one task ID or error code")
    row = await session.scalar(
        select(BackgroundJob)
        .where(
            BackgroundJob.id == claim.task.background_job_id,
            BackgroundJob.status == BackgroundJobStatus.DISPATCHING,
            BackgroundJob.dispatch_token == claim.dispatch_token,
        )
        .with_for_update()
    )
    if row is None:
        return False
    row.dispatch_token = None
    row.dispatch_locked_until = None
    if error_code is None:
        row.status = BackgroundJobStatus.QUEUED
        row.provider_task_id = provider_task_id
    else:
        row.status = BackgroundJobStatus.FAILED
        row.last_error_code = error_code
        row.completed_at = completed_at or utc_now()
    await session.flush()
    return True


async def dispatch_background_jobs_once(
    factory: async_sessionmaker[AsyncSession],
    task_queue: TaskQueuePort,
    *,
    queue_name: str,
    handler: str,
    now: datetime | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    enqueue_timeout_seconds: float = DEFAULT_ENQUEUE_TIMEOUT_SECONDS,
) -> DispatchResult:
    if enqueue_timeout_seconds <= 0:
        raise ValueError("enqueue_timeout_seconds must be positive")
    if lease_seconds <= enqueue_timeout_seconds:
        raise ValueError("lease_seconds must exceed enqueue_timeout_seconds")
    operation_time = now or utc_now()
    async with factory.begin() as session:
        claims = await claim_background_jobs(
            session,
            now=operation_time,
            limit=batch_size,
            lease_seconds=lease_seconds,
        )
    if not claims:
        return DispatchResult(claimed=0, queued=0, failed=0)

    async def enqueue(claim: ClaimedBackgroundJob) -> str | Exception:
        try:
            return await task_queue.enqueue(
                queue_name=queue_name,
                handler=handler,
                payload=claim.task.model_dump(mode="json"),
                idempotency_key=IdempotencyKey(
                    f"background-job:{claim.task.background_job_id}:"
                    f"attempt:{claim.task.attempt_count}"
                ),
                schedule_at=None,
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
                recorded = await finalize_background_job_dispatch(
                    session,
                    claim,
                    error_code=error_code,
                    completed_at=operation_time if now is not None else utc_now(),
                )
                failed += int(recorded)
            else:
                recorded = await finalize_background_job_dispatch(
                    session,
                    claim,
                    provider_task_id=outcome,
                )
                queued += int(recorded)
    return DispatchResult(claimed=len(claims), queued=queued, failed=failed)


def _work(row: BackgroundJob) -> MediaDeletionWorkV1:
    return MediaDeletionWorkV1(
        background_job_id=BackgroundJobId(row.id),
        attempt_count=row.attempt_count,
        move_job_id=JobId(row.move_job_id),
        media_asset_id=MediaAssetId(row.media_asset_id),
        object_key=row.target_object_key,
        generation=row.target_generation,
    )


async def start_media_deletion(
    session: AsyncSession,
    task: MediaDeletionTaskV1,
    *,
    now: datetime | None = None,
    execution_timeout_seconds: int = DEFAULT_EXECUTION_TIMEOUT_SECONDS,
) -> MediaDeletionWorkV1 | None:
    if execution_timeout_seconds <= 0:
        raise ValueError("execution_timeout_seconds must be positive")
    row = await session.scalar(
        select(BackgroundJob).where(BackgroundJob.id == task.background_job_id).with_for_update()
    )
    if (
        row is None
        or row.job_type is not BackgroundJobType.MEDIA_RETENTION_DELETE
        or row.attempt_count != task.attempt_count
        or row.trace_id != task.trace_id
    ):
        raise BackgroundJobNotFoundError(task.background_job_id)
    if row.status in {BackgroundJobStatus.SUCCEEDED, BackgroundJobStatus.FAILED}:
        return None
    if row.status is BackgroundJobStatus.RUNNING:
        return _work(row)
    if row.status not in {
        BackgroundJobStatus.DISPATCHING,
        BackgroundJobStatus.QUEUED,
    }:
        raise BackgroundJobConflictError(task.background_job_id)
    row.dispatch_token = None
    row.dispatch_locked_until = None
    row.status = BackgroundJobStatus.RUNNING
    row.execution_deadline_at = (now or utc_now()) + timedelta(seconds=execution_timeout_seconds)
    await session.flush()
    return _work(row)


async def complete_media_deletion(
    session: AsyncSession,
    result: MediaDeletionResultV1,
    *,
    completed_at: datetime | None = None,
) -> BackgroundJobResponse:
    selected = (
        await session.execute(
            select(BackgroundJob, MediaAsset)
            .join(MediaAsset, MediaAsset.id == BackgroundJob.media_asset_id)
            .where(BackgroundJob.id == result.background_job_id)
            .with_for_update()
        )
    ).one_or_none()
    if selected is None:
        raise BackgroundJobNotFoundError(result.background_job_id)
    row, asset = selected
    if row.attempt_count != result.attempt_count:
        raise BackgroundJobConflictError(result.background_job_id)
    if row.status in {BackgroundJobStatus.SUCCEEDED, BackgroundJobStatus.FAILED}:
        same_result = (
            row.status is BackgroundJobStatus.SUCCEEDED
            and result.outcome is MediaDeletionOutcome.SUCCEEDED
        ) or (
            row.status is BackgroundJobStatus.FAILED
            and result.outcome is MediaDeletionOutcome.FAILED
            and result.error_kind is not None
            and row.last_error_code == result.error_kind.value
        )
        if not same_result:
            raise BackgroundJobConflictError(result.background_job_id)
        return _response(row)
    if row.status is not BackgroundJobStatus.RUNNING:
        raise BackgroundJobConflictError(result.background_job_id)

    operation_time = completed_at or utc_now()
    row.completed_at = operation_time
    row.execution_deadline_at = None
    if result.outcome is MediaDeletionOutcome.FAILED:
        assert result.error_kind is not None
        row.status = BackgroundJobStatus.FAILED
        row.last_error_code = result.error_kind.value
    else:
        row.status = BackgroundJobStatus.SUCCEEDED
        asset.status = MediaAssetStatus.DELETED
        enqueue_domain_event(
            session,
            DomainEventType.MEDIA_DELETED_V1,
            row.move_job_id,
            trace_id=row.trace_id,
            payload={
                "background_job_id": str(row.id),
                "media_asset_id": str(row.media_asset_id),
            },
            occurred_at=operation_time,
        )
    await session.flush()
    return _response(row)
