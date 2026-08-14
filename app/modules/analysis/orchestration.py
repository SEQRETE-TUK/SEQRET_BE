"""Analysis task enqueue and worker work-lookup orchestration.

``request_analysis`` enqueues a minimal analysis task; the durable run is
created lazily by the worker handler. ``build_analysis_request`` reconstructs
the provider-neutral request from A-approved READY inventory media, so object
keys never travel inside the queue message.
"""

from collections.abc import Sequence
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.ai import AnalysisContentType, AnalysisRequest, AnalysisTaskV1
from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.ports import TaskQueuePort
from app.contracts.primitives import (
    AnalysisRunId,
    CaptureSessionId,
    IdempotencyKey,
    MediaAssetId,
    TraceId,
)
from app.modules.capture.models import MediaAsset

DEFAULT_ENQUEUE_TIMEOUT_SECONDS = 10.0


class AnalysisInputsUnavailableError(RuntimeError):
    """Raised when a capture session has no analyzable inventory media."""


async def build_analysis_request(
    session: AsyncSession,
    *,
    analysis_run_id: AnalysisRunId,
    capture_session_id: CaptureSessionId,
    model_name: str,
    model_version: str,
    prompt_version: str,
) -> AnalysisRequest:
    """Reconstruct the analysis request from READY inventory media."""

    assets: Sequence[MediaAsset] = (
        (
            await session.execute(
                select(MediaAsset)
                .where(
                    MediaAsset.capture_session_id == capture_session_id,
                    MediaAsset.media_purpose == MediaPurpose.INVENTORY,
                    MediaAsset.status == MediaAssetStatus.READY,
                )
                .order_by(MediaAsset.created_at, MediaAsset.id)
            )
        )
        .scalars()
        .all()
    )
    if not assets:
        raise AnalysisInputsUnavailableError(str(capture_session_id))

    return AnalysisRequest(
        analysis_run_id=analysis_run_id,
        capture_session_id=capture_session_id,
        source_media_asset_ids=tuple(MediaAssetId(asset.id) for asset in assets),
        object_keys=tuple(asset.object_key for asset in assets),
        content_types=tuple(cast(AnalysisContentType, asset.content_type) for asset in assets),
        model_name=model_name,
        model_version=model_version,
        prompt_version=prompt_version,
    )


async def request_analysis(
    task_queue: TaskQueuePort,
    *,
    analysis_run_id: AnalysisRunId,
    capture_session_id: CaptureSessionId,
    trace_id: TraceId,
    queue_name: str,
    handler: str,
    attempt_count: int = 1,
    timeout_seconds: float = DEFAULT_ENQUEUE_TIMEOUT_SECONDS,
) -> str:
    """Enqueue one analysis task; the run is created by the worker handler."""

    task = AnalysisTaskV1(
        analysis_run_id=analysis_run_id,
        capture_session_id=capture_session_id,
        attempt_count=attempt_count,
        trace_id=trace_id,
    )
    return await task_queue.enqueue(
        queue_name=queue_name,
        handler=handler,
        payload=task.model_dump(mode="json"),
        idempotency_key=IdempotencyKey(f"analysis:{analysis_run_id}:attempt:{attempt_count}"),
        schedule_at=None,
        timeout_seconds=timeout_seconds,
    )
