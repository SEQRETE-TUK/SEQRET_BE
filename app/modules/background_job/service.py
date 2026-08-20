"""Media-retention job creation, dispatch, retry, and result commands."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
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
    MediaValidationOutcome,
    MediaValidationResultV1,
    MediaValidationTaskV1,
    MediaValidationWorkV1,
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

    task: MediaDeletionTaskV1 | MediaValidationTaskV1
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
    participant_id: UUID | None,
    *,
    retention_cutoff: datetime,
    trace_id: str,
    scheduled_at: datetime | None = None,
) -> BackgroundJobResponse:
    """Persist one immutable deletion target after server-side retention checks."""

    if retention_cutoff.tzinfo is None or retention_cutoff.utcoffset() is None:
        raise ValueError("retention_cutoff must include a timezone")
    if scheduled_at is not None and (
        scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None
    ):
        raise ValueError("scheduled_at must include a timezone")
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
        or asset.generation != asset.generation.strip()
    ):
        raise BackgroundJobConflictError(media_asset_id)

    return await create_media_deletion_background_job(
        session,
        asset,
        job_id,
        participant_id,
        trace_id=trace_id,
        scheduled_at=scheduled_at,
    )


async def create_media_deletion_background_job(
    session: AsyncSession,
    asset: MediaAsset,
    job_id: UUID,
    participant_id: UUID | None,
    *,
    trace_id: str,
    scheduled_at: datetime | None = None,
) -> BackgroundJobResponse:
    """Persist one generation-pinned media deletion intent."""

    generation = cast(str, asset.generation)

    row = BackgroundJob(
        move_job_id=job_id,
        media_asset_id=asset.id,
        job_type=BackgroundJobType.MEDIA_RETENTION_DELETE,
        target_object_key=asset.object_key,
        target_generation=generation,
        trace_id=trace_id,
        scheduled_at=scheduled_at or utc_now(),
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
                BackgroundJob.media_asset_id == asset.id,
            )
        )
        if concurrent is None:
            raise
        return _response(concurrent)

    await session.flush()
    return _response(row)


async def create_media_validation_background_job(
    session: AsyncSession,
    job_id: UUID,
    media_asset_id: UUID,
    participant_id: UUID,
    *,
    trace_id: str,
    scheduled_at: datetime | None = None,
) -> BackgroundJobResponse:
    """Persist exactly one generation-pinned validation intent for an uploaded asset."""

    asset = await session.scalar(
        select(MediaAsset).where(MediaAsset.id == media_asset_id).with_for_update()
    )
    if asset is None:
        raise BackgroundJobNotFoundError(media_asset_id)
    asset_job_id = await session.scalar(
        select(CaptureSession.job_id).where(CaptureSession.id == asset.capture_session_id)
    )
    if asset_job_id != job_id:
        raise BackgroundJobNotFoundError(media_asset_id)
    existing = await session.scalar(
        select(BackgroundJob).where(
            BackgroundJob.job_type == BackgroundJobType.MEDIA_VALIDATION,
            BackgroundJob.media_asset_id == asset.id,
        )
    )
    if existing is not None:
        return _response(existing)
    if (
        asset.status is not MediaAssetStatus.UPLOADED
        or not asset.generation
        or asset.generation != asset.generation.strip()
        or asset.actual_size_bytes is None
    ):
        raise BackgroundJobConflictError(asset.id)

    row = BackgroundJob(
        move_job_id=job_id,
        media_asset_id=asset.id,
        job_type=BackgroundJobType.MEDIA_VALIDATION,
        target_object_key=asset.object_key,
        target_generation=asset.generation,
        target_content_type=asset.content_type,
        target_size_bytes=asset.actual_size_bytes,
        trace_id=trace_id,
        scheduled_at=scheduled_at or utc_now(),
        created_by_participant_id=participant_id,
    )
    session.add(row)
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

    if row.job_type is BackgroundJobType.MEDIA_VALIDATION:
        asset = (
            await session.scalars(
                select(MediaAsset).where(MediaAsset.id == row.media_asset_id).with_for_update()
            )
        ).one()
        if execution_expired:
            if asset.status is not MediaAssetStatus.PROCESSING:
                raise BackgroundJobConflictError(background_job_id)
            asset.status = MediaAssetStatus.FAILED
            asset.sha256_hex = None

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
        task = (
            MediaValidationTaskV1(
                background_job_id=BackgroundJobId(row.id),
                attempt_count=row.attempt_count,
                trace_id=row.trace_id,
            )
            if row.job_type is BackgroundJobType.MEDIA_VALIDATION
            else MediaDeletionTaskV1(
                background_job_id=BackgroundJobId(row.id),
                attempt_count=row.attempt_count,
                trace_id=row.trace_id,
            )
        )
        claims.append(ClaimedBackgroundJob(task=task, dispatch_token=token))
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
        .where(BackgroundJob.id == claim.task.background_job_id)
        .with_for_update()
    )
    if row is None:
        return False
    if not (
        row.status is BackgroundJobStatus.DISPATCHING and row.dispatch_token == claim.dispatch_token
    ):
        already_accepted = (
            provider_task_id is not None
            and row.attempt_count == claim.task.attempt_count
            and row.trace_id == claim.task.trace_id
            and row.status
            in {
                BackgroundJobStatus.QUEUED,
                BackgroundJobStatus.RUNNING,
                BackgroundJobStatus.SUCCEEDED,
                BackgroundJobStatus.FAILED,
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


def _validation_work(row: BackgroundJob) -> MediaValidationWorkV1:
    return MediaValidationWorkV1(
        background_job_id=BackgroundJobId(row.id),
        attempt_count=row.attempt_count,
        object_key=row.target_object_key,
        source_generation=row.target_generation,
        expected_content_type=cast(str, row.target_content_type),
        expected_size_bytes=cast(int, row.target_size_bytes),
    )


async def start_media_validation(
    session: AsyncSession,
    task: MediaValidationTaskV1,
    *,
    now: datetime | None = None,
    execution_timeout_seconds: int = DEFAULT_EXECUTION_TIMEOUT_SECONDS,
) -> MediaValidationWorkV1 | None:
    """Start or replay one current validation attempt without exposing A-owned rows."""

    if execution_timeout_seconds <= 0:
        raise ValueError("execution_timeout_seconds must be positive")
    selected = (
        await session.execute(
            select(BackgroundJob, MediaAsset)
            .join(MediaAsset, MediaAsset.id == BackgroundJob.media_asset_id)
            .where(BackgroundJob.id == task.background_job_id)
            .with_for_update()
        )
    ).one_or_none()
    if selected is None:
        raise BackgroundJobNotFoundError(task.background_job_id)
    row, asset = selected
    if (
        row.job_type is not BackgroundJobType.MEDIA_VALIDATION
        or row.attempt_count != task.attempt_count
        or row.trace_id != task.trace_id
    ):
        raise BackgroundJobNotFoundError(task.background_job_id)
    if row.status in {BackgroundJobStatus.SUCCEEDED, BackgroundJobStatus.FAILED}:
        return None
    if row.status is BackgroundJobStatus.RUNNING:
        if asset.status is not MediaAssetStatus.PROCESSING:
            raise BackgroundJobConflictError(task.background_job_id)
        return _validation_work(row)
    if row.status not in {BackgroundJobStatus.DISPATCHING, BackgroundJobStatus.QUEUED}:
        raise BackgroundJobConflictError(task.background_job_id)
    if (
        asset.status not in {MediaAssetStatus.UPLOADED, MediaAssetStatus.FAILED}
        or asset.object_key != row.target_object_key
        or asset.generation != row.target_generation
        or asset.content_type != row.target_content_type
        or asset.actual_size_bytes != row.target_size_bytes
    ):
        raise BackgroundJobConflictError(task.background_job_id)

    row.dispatch_token = None
    row.dispatch_locked_until = None
    row.status = BackgroundJobStatus.RUNNING
    row.execution_deadline_at = (now or utc_now()) + timedelta(seconds=execution_timeout_seconds)
    asset.status = MediaAssetStatus.PROCESSING
    asset.sha256_hex = None
    await session.flush()
    return _validation_work(row)


async def complete_media_validation(
    session: AsyncSession,
    result: MediaValidationResultV1,
    *,
    completed_at: datetime | None = None,
) -> BackgroundJobResponse:
    """Apply one attempt-scoped validation result and asset state atomically."""

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
    if (
        row.job_type is not BackgroundJobType.MEDIA_VALIDATION
        or row.attempt_count != result.attempt_count
        or row.target_generation != result.source_generation
        or asset.object_key != row.target_object_key
        or asset.generation != row.target_generation
        or asset.content_type != row.target_content_type
        or asset.actual_size_bytes != row.target_size_bytes
    ):
        raise BackgroundJobConflictError(result.background_job_id)
    if result.outcome is MediaValidationOutcome.SUCCEEDED and (
        result.observed_content_type != row.target_content_type
        or result.observed_size_bytes != row.target_size_bytes
    ):
        raise BackgroundJobConflictError(result.background_job_id)
    if row.status in {BackgroundJobStatus.SUCCEEDED, BackgroundJobStatus.FAILED}:
        same_result = (
            row.status is BackgroundJobStatus.SUCCEEDED
            and result.outcome is MediaValidationOutcome.SUCCEEDED
            and asset.sha256_hex == result.sha256_hex
        ) or (
            row.status is BackgroundJobStatus.FAILED
            and result.outcome is MediaValidationOutcome.FAILED
            and result.error_kind is not None
            and row.last_error_code == result.error_kind.value
        )
        if not same_result:
            raise BackgroundJobConflictError(result.background_job_id)
        return _response(row)
    if (
        row.status is not BackgroundJobStatus.RUNNING
        or asset.status is not MediaAssetStatus.PROCESSING
    ):
        raise BackgroundJobConflictError(result.background_job_id)

    row.completed_at = completed_at or utc_now()
    row.execution_deadline_at = None
    if result.outcome is MediaValidationOutcome.FAILED:
        assert result.error_kind is not None
        row.status = BackgroundJobStatus.FAILED
        row.last_error_code = result.error_kind.value
        asset.status = MediaAssetStatus.FAILED
        asset.sha256_hex = None
    else:
        assert result.sha256_hex is not None
        row.status = BackgroundJobStatus.SUCCEEDED
        asset.status = MediaAssetStatus.READY
        asset.generation = result.source_generation
        asset.actual_size_bytes = cast(int, result.observed_size_bytes)
        asset.sha256_hex = result.sha256_hex
    await session.flush()
    return _response(row)


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
    if (
        row.job_type is not BackgroundJobType.MEDIA_RETENTION_DELETE
        or row.attempt_count != result.attempt_count
    ):
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
