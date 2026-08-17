"""Analysis task enqueue and worker work-lookup orchestration.

``request_analysis`` enqueues a minimal analysis task; the durable run is
created lazily by the worker handler. ``build_analysis_request`` reconstructs
the provider-neutral request from A-approved READY inventory and condition
media, so object keys never travel inside the queue message.
"""

from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.ai import (
    AnalysisContentType,
    AnalysisLocationKind,
    AnalysisRequest,
    AnalysisSourceContext,
    AnalysisTaskV1,
)
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
from app.modules.move_job.models import Location, RoomZone

DEFAULT_ENQUEUE_TIMEOUT_SECONDS = 10.0


class AnalysisInputsUnavailableError(RuntimeError):
    """Raised when a capture session has no analyzable scope media."""


async def build_analysis_request(
    session: AsyncSession,
    *,
    analysis_run_id: AnalysisRunId,
    capture_session_id: CaptureSessionId,
    model_name: str,
    model_version: str,
    prompt_version: str,
) -> AnalysisRequest:
    """Reconstruct a v2 analysis request from READY scope media."""

    source_rows = (
        await session.execute(
            select(MediaAsset, RoomZone.location_id, Location.kind)
            .join(RoomZone, RoomZone.id == MediaAsset.room_zone_id)
            .join(Location, Location.id == RoomZone.location_id)
            .where(
                MediaAsset.capture_session_id == capture_session_id,
                MediaAsset.media_purpose.in_({MediaPurpose.INVENTORY, MediaPurpose.CONDITION}),
                MediaAsset.status == MediaAssetStatus.READY,
            )
            .order_by(MediaAsset.created_at, MediaAsset.id)
        )
    ).all()
    if not source_rows:
        raise AnalysisInputsUnavailableError(str(capture_session_id))
    assets = tuple(row[0] for row in source_rows)

    return AnalysisRequest(
        analysis_run_id=analysis_run_id,
        capture_session_id=capture_session_id,
        source_media_asset_ids=tuple(MediaAssetId(asset.id) for asset in assets),
        object_keys=tuple(asset.object_key for asset in assets),
        content_types=tuple(cast(AnalysisContentType, asset.content_type) for asset in assets),
        model_name=model_name,
        model_version=model_version,
        prompt_version=prompt_version,
        requested_result_schema_version=2,
        source_contexts=tuple(
            AnalysisSourceContext(
                media_asset_id=MediaAssetId(asset.id),
                location_id=location_id,
                location_kind=cast(AnalysisLocationKind, location_kind.value),
                room_zone_id=asset.room_zone_id,
            )
            for asset, location_id, location_kind in source_rows
        ),
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
