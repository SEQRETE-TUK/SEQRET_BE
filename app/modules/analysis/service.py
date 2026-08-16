"""Analysis run orchestration that only ever produces editable drafts.

The functions here persist ``ai_analysis_run`` and ``detection`` rows and
rebuild the provider-neutral :class:`AnalysisResult`. They deliberately never
build a ``scope_version``; track A's ``ImportAnalysisDraft`` command turns an
:class:`AnalysisResult` into an editable scope draft. Each command operates on a
caller-managed session so a worker owns the transaction and retry policy.
"""

from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.ai import AnalysisResult, DraftItem
from app.contracts.ports import ProviderErrorKind
from app.contracts.primitives import (
    AnalysisRunId,
    CaptureSessionId,
    MediaAssetId,
    TraceId,
)
from app.modules.analysis.models import AiAnalysisRun, AnalysisRunStatus, Detection


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
                    item_key=item.item_key,
                    description=item.description,
                    confidence=item.confidence,
                    review_required=review_required,
                    source_media_asset_ids=[
                        str(asset_id) for asset_id in item.source_media_asset_ids
                    ],
                )
            )
            ordinal += 1
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
            confidence=detection.confidence,
            source_media_asset_ids=tuple(
                MediaAssetId(UUID(asset_id)) for asset_id in detection.source_media_asset_ids
            ),
        )
        (review_required_items if detection.review_required else draft_items).append(item)

    return AnalysisResult(
        analysis_run_id=AnalysisRunId(run.id),
        capture_session_id=CaptureSessionId(run.capture_session_id),
        model_name=cast(str, run.model_name),
        model_version=cast(str, run.model_version),
        prompt_version=cast(str, run.prompt_version),
        draft_items=tuple(draft_items),
        review_required_items=tuple(review_required_items),
    )


async def get_analysis_run_status(
    session: AsyncSession,
    *,
    analysis_run_id: AnalysisRunId,
) -> AnalysisRunStatus | None:
    """Return the current run status, or ``None`` when the run is absent."""

    run = await _load_run(session, analysis_run_id)
    return None if run is None else run.status


async def get_analysis_run_attempt_count(
    session: AsyncSession,
    *,
    analysis_run_id: AnalysisRunId,
) -> int | None:
    """Return the current attempt count, or ``None`` when the run is absent."""

    run = await _load_run(session, analysis_run_id)
    return None if run is None else run.attempt_count


async def get_analysis_run_failure(
    session: AsyncSession,
    *,
    analysis_run_id: AnalysisRunId,
) -> ProviderErrorKind | None:
    """Return the persisted provider failure kind, or ``None`` when unset."""

    run = await _load_run(session, analysis_run_id)
    if run is None or run.failure_code is None:
        return None
    return ProviderErrorKind(run.failure_code)


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
