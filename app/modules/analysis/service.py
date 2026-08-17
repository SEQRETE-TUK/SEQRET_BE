"""Analysis run orchestration that only ever produces editable drafts.

The functions here persist ``ai_analysis_run`` and ``detection`` rows and
rebuild the provider-neutral :class:`AnalysisResult`. They deliberately never
build a ``scope_version``; track A's ``ImportAnalysisDraft`` command turns an
:class:`AnalysisResult` into an editable scope draft. Each command operates on a
caller-managed session so a worker owns the transaction and retry policy.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.ai import (
    AnalysisCarryDistanceCondition,
    AnalysisElevatorAvailability,
    AnalysisFloorCondition,
    AnalysisKnowledgeStatus,
    AnalysisLocationConditionField,
    AnalysisLocationKind,
    AnalysisParkingAccess,
    AnalysisResidenceType,
    AnalysisResult,
    AnalysisStairUsage,
    DraftItem,
    DraftLocationCondition,
)
from app.contracts.ports import ProviderErrorKind
from app.contracts.primitives import (
    AnalysisRunId,
    CaptureSessionId,
    MediaAssetId,
    TraceId,
)
from app.modules.analysis.models import (
    AiAnalysisRun,
    AnalysisLocationConditionSuggestion,
    AnalysisRunStatus,
    Detection,
)


class AnalysisRetryDecision(StrEnum):
    """Whether a redelivery should be retried or finalized as terminal."""

    RETRY = "retry"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class AnalysisRunSnapshot:
    """A single-read view of a run's status and persisted failure kind.

    Reading both from one row (one ``SELECT``) makes the pair atomic, so a
    concurrent reopen cannot land between a status read and a failure read and
    misreport a still-retryable failure as ``None``/non-retryable.
    """

    status: AnalysisRunStatus
    failure_kind: ProviderErrorKind | None


class AnalysisRunNotFoundError(RuntimeError):
    """Raised when a completion or failure targets a missing analysis run."""


class AnalysisRunConflictError(RuntimeError):
    """Raised when an outcome contradicts the stored analysis run."""


async def _load_run(session: AsyncSession, analysis_run_id: AnalysisRunId) -> AiAnalysisRun | None:
    return await session.get(AiAnalysisRun, analysis_run_id)


async def _load_run_for_update(
    session: AsyncSession,
    analysis_run_id: AnalysisRunId,
) -> AiAnalysisRun | None:
    return cast(
        AiAnalysisRun | None,
        await session.scalar(
            select(AiAnalysisRun).where(AiAnalysisRun.id == analysis_run_id).with_for_update()
        ),
    )


async def start_analysis_run(
    session: AsyncSession,
    *,
    analysis_run_id: AnalysisRunId,
    capture_session_id: CaptureSessionId,
    trace_id: TraceId,
    now: datetime,
) -> None:
    """Create a ``RUNNING`` run; a same-capture replay is a no-op."""

    run = await _load_run_for_update(session, analysis_run_id)
    if run is None:
        try:
            async with session.begin_nested():
                session.add(
                    AiAnalysisRun(
                        id=analysis_run_id,
                        capture_session_id=capture_session_id,
                        status=AnalysisRunStatus.RUNNING,
                        attempt_count=1,
                        trace_id=trace_id,
                        started_at=now,
                    )
                )
                await session.flush()
        except IntegrityError:
            run = await _load_run_for_update(session, analysis_run_id)
            if run is None:
                raise
        else:
            return

    if run.capture_session_id != capture_session_id:
        raise AnalysisRunConflictError("analysis run ID belongs to another capture session")

    return


async def complete_analysis_run(
    session: AsyncSession,
    *,
    result: AnalysisResult,
    now: datetime,
) -> None:
    """Persist detections and mark the run ``COMPLETED``; replay is a no-op."""

    run = await _load_run_for_update(session, result.analysis_run_id)
    if run is None:
        raise AnalysisRunNotFoundError(str(result.analysis_run_id))

    if run.status is AnalysisRunStatus.COMPLETED:
        if await load_analysis_result(session, analysis_run_id=result.analysis_run_id) == result:
            return
        raise AnalysisRunConflictError("completed run does not match the replayed result")

    if run.status is not AnalysisRunStatus.RUNNING:
        raise AnalysisRunConflictError("only a running analysis run can complete")
    if run.capture_session_id != result.capture_session_id:
        raise AnalysisRunConflictError("result capture session does not match the run")

    run.status = AnalysisRunStatus.COMPLETED
    run.model_name = result.model_name
    run.model_version = result.model_version
    run.prompt_version = result.prompt_version
    run.result_schema_version = result.result_schema_version
    run.failure_code = None
    run.completed_at = now

    ordinal = 0
    for review_required, items in (
        (False, result.draft_items),
        (True, result.review_required_items),
    ):
        for item in items:
            session.add(
                Detection(
                    analysis_run_id=result.analysis_run_id,
                    ordinal=ordinal,
                    item_schema_version=result.result_schema_version,
                    item_key=item.item_key,
                    description=item.description,
                    name=item.name,
                    quantity=item.quantity,
                    unit=item.unit,
                    work_note=item.work_note,
                    confidence=item.confidence,
                    review_required=review_required,
                    source_media_asset_ids=[
                        str(asset_id) for asset_id in item.source_media_asset_ids
                    ],
                )
            )
            ordinal += 1
    for ordinal, suggestion in enumerate(result.location_condition_suggestions):
        session.add(
            AnalysisLocationConditionSuggestion(
                analysis_run_id=result.analysis_run_id,
                ordinal=ordinal,
                location_id=suggestion.location_id,
                location_kind=suggestion.location_kind,
                residence_type=suggestion.residence_type,
                floor_status=suggestion.floor.status,
                floor_value=suggestion.floor.value,
                elevator=suggestion.elevator,
                stairs=suggestion.stairs,
                parking_access=suggestion.parking_access,
                carry_distance_status=suggestion.carry_distance.status,
                carry_distance_value_m=suggestion.carry_distance.value_m,
                access_note=suggestion.access_note,
                confidence=suggestion.confidence,
                review_required_fields=list(suggestion.review_required_fields),
                source_media_asset_ids=[
                    str(asset_id) for asset_id in suggestion.source_media_asset_ids
                ],
            )
        )
    await session.flush()


async def fail_analysis_run(
    session: AsyncSession,
    *,
    analysis_run_id: AnalysisRunId,
    error_kind: ProviderErrorKind,
    now: datetime,
) -> None:
    """Record a provider failure so a human can still work manually."""

    run = await _load_run_for_update(session, analysis_run_id)
    if run is None:
        raise AnalysisRunNotFoundError(str(analysis_run_id))

    if run.status is AnalysisRunStatus.FAILED:
        if run.failure_code == error_kind.value:
            return
        raise AnalysisRunConflictError("failed run does not match the replayed error")
    if run.status is not AnalysisRunStatus.RUNNING:
        raise AnalysisRunConflictError("only a running analysis run can fail")

    run.status = AnalysisRunStatus.FAILED
    run.failure_code = error_kind.value
    run.completed_at = now
    await session.flush()


async def load_analysis_result(
    session: AsyncSession,
    *,
    analysis_run_id: AnalysisRunId,
) -> AnalysisResult | None:
    """Rebuild the completed draft result, or ``None`` if not yet completed."""

    run = await _load_run(session, analysis_run_id)
    if run is None or run.status is not AnalysisRunStatus.COMPLETED:
        return None

    detections = (
        await session.execute(
            select(Detection)
            .where(Detection.analysis_run_id == analysis_run_id)
            .order_by(Detection.ordinal)
        )
    ).scalars()

    draft_items: list[DraftItem] = []
    review_required_items: list[DraftItem] = []
    for detection in detections:
        item = DraftItem(
            item_key=detection.item_key,
            description=detection.description,
            name=detection.name,
            quantity=detection.quantity,
            unit=detection.unit,
            work_note=detection.work_note,
            confidence=detection.confidence,
            source_media_asset_ids=tuple(
                MediaAssetId(UUID(asset_id)) for asset_id in detection.source_media_asset_ids
            ),
        )
        (review_required_items if detection.review_required else draft_items).append(item)

    suggestions = (
        await session.execute(
            select(AnalysisLocationConditionSuggestion)
            .where(AnalysisLocationConditionSuggestion.analysis_run_id == analysis_run_id)
            .order_by(AnalysisLocationConditionSuggestion.ordinal)
        )
    ).scalars()
    location_condition_suggestions = tuple(
        DraftLocationCondition(
            location_id=suggestion.location_id,
            location_kind=cast(AnalysisLocationKind, suggestion.location_kind),
            residence_type=cast(AnalysisResidenceType, suggestion.residence_type),
            floor=AnalysisFloorCondition(
                status=cast(AnalysisKnowledgeStatus, suggestion.floor_status),
                value=suggestion.floor_value,
            ),
            elevator=cast(AnalysisElevatorAvailability, suggestion.elevator),
            stairs=cast(AnalysisStairUsage, suggestion.stairs),
            parking_access=cast(AnalysisParkingAccess, suggestion.parking_access),
            carry_distance=AnalysisCarryDistanceCondition(
                status=cast(AnalysisKnowledgeStatus, suggestion.carry_distance_status),
                value_m=suggestion.carry_distance_value_m,
            ),
            access_note=suggestion.access_note,
            confidence=suggestion.confidence,
            review_required_fields=cast(
                tuple[AnalysisLocationConditionField, ...],
                tuple(suggestion.review_required_fields),
            ),
            source_media_asset_ids=tuple(
                MediaAssetId(UUID(asset_id)) for asset_id in suggestion.source_media_asset_ids
            ),
        )
        for suggestion in suggestions
    )

    return AnalysisResult(
        analysis_run_id=AnalysisRunId(run.id),
        capture_session_id=CaptureSessionId(run.capture_session_id),
        model_name=cast(str, run.model_name),
        model_version=cast(str, run.model_version),
        prompt_version=cast(str, run.prompt_version),
        result_schema_version=cast(Literal[1, 2], run.result_schema_version),
        draft_items=tuple(draft_items),
        review_required_items=tuple(review_required_items),
        location_condition_suggestions=location_condition_suggestions,
    )


async def get_analysis_run_snapshot(
    session: AsyncSession,
    *,
    analysis_run_id: AnalysisRunId,
) -> AnalysisRunSnapshot:
    """Return the run's status and failure kind from a single row read.

    Both fields come from one ``SELECT`` so a concurrent reopen cannot slip
    between a status read and a failure read. The run is expected to exist (a
    caller leases or creates it first); an absent run is an invariant violation.
    """

    run = await _load_run(session, analysis_run_id)
    if run is None:
        raise AnalysisRunNotFoundError(str(analysis_run_id))
    failure_kind = None if run.failure_code is None else ProviderErrorKind(run.failure_code)
    return AnalysisRunSnapshot(status=run.status, failure_kind=failure_kind)


async def reopen_analysis_run(
    session: AsyncSession,
    *,
    analysis_run_id: AnalysisRunId,
    now: datetime,
) -> None:
    """Open a new ``RUNNING`` attempt for a failed run (explicit retry).

    A run already ``RUNNING`` is a no-op so a duplicate retry converges, and a
    ``COMPLETED`` run is never reopened.
    """

    run = await _load_run_for_update(session, analysis_run_id)
    if run is None:
        raise AnalysisRunNotFoundError(str(analysis_run_id))

    if run.status is AnalysisRunStatus.RUNNING:
        return
    if run.status is not AnalysisRunStatus.FAILED:
        raise AnalysisRunConflictError("only a failed analysis run can be retried")

    run.status = AnalysisRunStatus.RUNNING
    run.attempt_count += 1
    run.started_at = now
    run.completed_at = None
    run.failure_code = None
    run.model_name = None
    run.model_version = None
    run.prompt_version = None
    run.result_schema_version = None
    await session.flush()


async def prepare_analysis_retry(
    session: AsyncSession,
    *,
    analysis_run_id: AnalysisRunId,
    max_attempts: int,
    now: datetime,
) -> AnalysisRetryDecision:
    """Atomically snapshot the run and prepare a bounded retry under a row lock.

    Concurrent redeliveries serialize on the run row: a ``FAILED`` run below the
    attempt limit reopens a fresh ``RUNNING`` attempt and returns ``RETRY``. A
    later delivery that finds the run already reopened (``RUNNING``) — or in any
    other non-terminal state — also returns ``RETRY``, so it never finalizes the
    dispatch the first delivery already prepared for retry. Only an absent run or
    a ``FAILED`` run that has exhausted ``max_attempts`` is ``TERMINAL``.
    """

    run = await _load_run_for_update(session, analysis_run_id)
    if run is None:
        return AnalysisRetryDecision.TERMINAL
    if run.status is AnalysisRunStatus.FAILED:
        if run.attempt_count >= max_attempts:
            return AnalysisRetryDecision.TERMINAL
        run.status = AnalysisRunStatus.RUNNING
        run.attempt_count += 1
        run.started_at = now
        run.completed_at = None
        run.failure_code = None
        run.model_name = None
        run.model_version = None
        run.prompt_version = None
        run.result_schema_version = None
        await session.flush()
    return AnalysisRetryDecision.RETRY
