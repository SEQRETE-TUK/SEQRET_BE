"""A-owned query and command for customer review of an AI scope draft."""

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.ai import AnalysisResult, DraftItem
from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.ports import StoragePort, validate_storage_url
from app.contracts.primitives import utc_now
from app.modules.analysis_review.schemas import (
    AnalysisReviewComplete,
    AnalysisReviewItem,
    AnalysisReviewResponse,
    AnalysisReviewVideoPreview,
    AnalysisReviewZone,
)
from app.modules.analysis_workflow.models import (
    CaptureAnalysisDispatch,
    CaptureAnalysisStatus,
)
from app.modules.background_job.service import create_media_deletion_background_job
from app.modules.capture.models import MediaAsset
from app.modules.capture.service import STORAGE_TIMEOUT_SECONDS
from app.modules.move_job.models import Location, LocationKind, RoomZone
from app.modules.scope.models import ScopeVersion
from app.modules.scope.schemas import (
    ScopeContent,
    ScopeItem,
    ScopeItemReviewStatus,
    ScopeItemSource,
    ScopeItemV2,
    ScopeVersionCreate,
)
from app.modules.scope.service import (
    ScopeResourceNotFoundError,
    ScopeVersionConflictError,
    _normalize_scope_content,
    _with_location_condition_snapshot,
    create_scope_version,
)


class AnalysisReviewNotFoundError(LookupError):
    """Raised when the caller has no matching analysis review."""


class AnalysisReviewConflictError(ValueError):
    """Raised when analysis or review state is not current and editable."""


READ_URL_TTL_SECONDS = 5 * 60


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def _latest_owned_analysis(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
    *,
    lock: bool = False,
) -> CaptureAnalysisDispatch:
    statement = (
        select(CaptureAnalysisDispatch)
        .where(
            CaptureAnalysisDispatch.move_job_id == job_id,
            CaptureAnalysisDispatch.submitted_by_participant_id == participant_id,
        )
        .order_by(
            CaptureAnalysisDispatch.submitted_at.desc(),
            CaptureAnalysisDispatch.analysis_run_id.desc(),
        )
        .limit(1)
    )
    if lock:
        statement = statement.with_for_update()
    row = await session.scalar(statement)
    if row is None:
        raise AnalysisReviewNotFoundError(job_id)
    if row.status is not CaptureAnalysisStatus.COMPLETED or row.scope_version_id is None:
        raise AnalysisReviewConflictError(row.analysis_run_id)
    return row


async def _load_source_scope(
    session: AsyncSession,
    row: CaptureAnalysisDispatch,
) -> tuple[ScopeVersion, AnalysisResult]:
    source = await session.scalar(
        select(ScopeVersion).where(
            ScopeVersion.id == row.scope_version_id,
            ScopeVersion.job_id == row.move_job_id,
            ScopeVersion.source_analysis_run_id == row.analysis_run_id,
            ScopeVersion.source_capture_session_id == row.capture_session_id,
        )
    )
    if source is None or source.analysis_source is None:
        raise AnalysisReviewConflictError(row.analysis_run_id)
    return source, AnalysisResult.model_validate(source.analysis_source, strict=False)


async def _child_scope(
    session: AsyncSession,
    source_scope_version_id: UUID,
) -> ScopeVersion | None:
    review: ScopeVersion | None = await session.scalar(
        select(ScopeVersion).where(ScopeVersion.parent_version_id == source_scope_version_id)
    )
    return review


def _current_items(
    source_result: AnalysisResult,
    content: ScopeContent,
) -> tuple[AnalysisReviewItem, ...]:
    ai_items: dict[str, DraftItem] = {
        item.item_key: item
        for item in source_result.draft_items + source_result.review_required_items
    }
    review_required_keys = {item.item_key for item in source_result.review_required_items}
    return tuple(
        AnalysisReviewItem(
            item_key=item.item_key,
            room_zone_id=item.room_zone_id,
            description=(item.description if isinstance(item, ScopeItem) else item.name),
            name=(item.description if isinstance(item, ScopeItem) else item.name),
            quantity=(None if isinstance(item, ScopeItem) else item.quantity),
            unit=(None if isinstance(item, ScopeItem) else item.unit),
            work_note=(None if isinstance(item, ScopeItem) else item.work_note),
            review_status=(
                (
                    ScopeItemReviewStatus.REVIEW_REQUIRED
                    if item.item_key in review_required_keys
                    else ScopeItemReviewStatus.CONFIRMED
                )
                if isinstance(item, ScopeItem)
                else item.review_status
            ),
            scope_source=(
                (ScopeItemSource.AI if item.item_key in ai_items else ScopeItemSource.CUSTOMER)
                if isinstance(item, ScopeItem)
                else item.source
            ),
            source=(
                "ai"
                if (
                    (isinstance(item, ScopeItem) and item.item_key in ai_items)
                    or (isinstance(item, ScopeItemV2) and item.source is ScopeItemSource.AI)
                )
                else "customer"
            ),
            confidence=(ai_items[item.item_key].confidence if item.item_key in ai_items else None),
            review_required=(
                item.item_key in review_required_keys
                if isinstance(item, ScopeItem)
                else item.review_status is ScopeItemReviewStatus.REVIEW_REQUIRED
            ),
            source_media_asset_ids=(
                tuple(ai_items[item.item_key].source_media_asset_ids)
                if item.item_key in ai_items
                else ()
            ),
        )
        for item in content.items
    )


async def _zone_views(
    session: AsyncSession,
    job_id: UUID,
    capture_session_id: UUID,
) -> tuple[AnalysisReviewZone, ...]:
    zones = (
        await session.scalars(
            select(RoomZone)
            .join(Location, Location.id == RoomZone.location_id)
            .where(
                Location.job_id == job_id,
                Location.kind == LocationKind.ORIGIN,
            )
            .order_by(RoomZone.sort_order, RoomZone.id)
        )
    ).all()
    assets = (
        await session.scalars(
            select(MediaAsset).where(
                MediaAsset.capture_session_id == capture_session_id,
                MediaAsset.media_purpose == MediaPurpose.INVENTORY,
            )
        )
    ).all()
    total_counts = Counter(asset.room_zone_id for asset in assets)
    ready_counts = Counter(
        asset.room_zone_id for asset in assets if asset.status is MediaAssetStatus.READY
    )
    failed_counts = Counter(
        asset.room_zone_id for asset in assets if asset.status is MediaAssetStatus.FAILED
    )
    return tuple(
        AnalysisReviewZone(
            room_zone_id=zone.id,
            name=zone.name,
            sort_order=zone.sort_order,
            total_media_count=total_counts[zone.id],
            ready_media_count=ready_counts[zone.id],
            failed_media_count=failed_counts[zone.id],
        )
        for zone in zones
    )


async def _video_preview(
    session: AsyncSession,
    storage: StoragePort,
    capture_session_id: UUID,
) -> AnalysisReviewVideoPreview | None:
    asset = await session.scalar(
        select(MediaAsset)
        .where(
            MediaAsset.capture_session_id == capture_session_id,
            MediaAsset.media_purpose == MediaPurpose.INVENTORY,
            MediaAsset.status == MediaAssetStatus.READY,
            MediaAsset.content_type == "video/mp4",
            MediaAsset.generation.is_not(None),
        )
        .order_by(MediaAsset.created_at, MediaAsset.id)
        .limit(1)
    )
    if asset is None:
        return None
    assert asset.generation is not None
    read_url = await storage.create_read_url(
        object_key=asset.object_key,
        generation=asset.generation,
        expires_in_seconds=READ_URL_TTL_SECONDS,
        timeout_seconds=STORAGE_TIMEOUT_SECONDS,
    )
    return AnalysisReviewVideoPreview(
        media_asset_id=asset.id,
        content_type=asset.content_type,
        read_url=validate_storage_url(
            read_url,
            "storage returned an invalid read URL",
        ),
        expires_at=utc_now() + timedelta(seconds=READ_URL_TTL_SECONDS),
    )


async def _consume_review_videos(
    session: AsyncSession,
    row: CaptureAnalysisDispatch,
    participant_id: UUID,
) -> None:
    """Hide accepted analysis videos and enqueue generation-pinned deletion."""

    assets = (
        await session.scalars(
            select(MediaAsset)
            .where(
                MediaAsset.capture_session_id == row.capture_session_id,
                MediaAsset.media_purpose == MediaPurpose.INVENTORY,
                MediaAsset.content_type == "video/mp4",
                MediaAsset.status.in_({MediaAssetStatus.READY, MediaAssetStatus.DELETED}),
                MediaAsset.generation.is_not(None),
            )
            .with_for_update()
        )
    ).all()
    for asset in assets:
        await create_media_deletion_background_job(
            session,
            asset,
            row.move_job_id,
            participant_id,
            trace_id=row.trace_id,
        )
        asset.status = MediaAssetStatus.DELETED
    await session.flush()


async def _to_response(
    session: AsyncSession,
    row: CaptureAnalysisDispatch,
    source: ScopeVersion,
    source_result: AnalysisResult,
    review: ScopeVersion | None,
    storage: StoragePort | None = None,
) -> AnalysisReviewResponse:
    if row.completed_at is None:
        raise AnalysisReviewConflictError(row.analysis_run_id)
    content_row = review or source
    content = ScopeContent.model_validate(content_row.content, strict=False)
    return AnalysisReviewResponse(
        job_id=row.move_job_id,
        analysis_run_id=row.analysis_run_id,
        capture_session_id=row.capture_session_id,
        source_scope_version_id=source.id,
        review_scope_version_id=review.id if review else None,
        scope_schema_version=content.schema_version,
        analysis_completed_at=_aware(row.completed_at),
        review_completed_at=_aware(review.created_at) if review else None,
        zones=await _zone_views(session, row.move_job_id, row.capture_session_id),
        items=_current_items(source_result, content),
        location_conditions=content.location_conditions,
        location_condition_suggestions=source_result.location_condition_suggestions,
        video_preview=(
            await _video_preview(session, storage, row.capture_session_id)
            if storage is not None
            else None
        ),
    )


async def get_analysis_review(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
    *,
    storage: StoragePort | None = None,
) -> AnalysisReviewResponse:
    """Return the latest completed analysis owned by the customer."""

    row = await _latest_owned_analysis(session, job_id, participant_id)
    source, source_result = await _load_source_scope(session, row)
    review = await _child_scope(session, source.id)
    if review is not None and review.created_by_participant_id != participant_id:
        raise AnalysisReviewConflictError(source.id)
    return await _to_response(session, row, source, source_result, review, storage)


async def complete_analysis_review(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
    command: AnalysisReviewComplete,
) -> AnalysisReviewResponse:
    """Create one immutable customer edit, with same-payload replay support."""

    row = await _latest_owned_analysis(session, job_id, participant_id, lock=True)
    source, source_result = await _load_source_scope(session, row)
    if command.source_scope_version_id != source.id:
        raise AnalysisReviewConflictError(command.source_scope_version_id)

    requested_items: tuple[ScopeItem | ScopeItemV2, ...]
    if command.scope_schema_version == 1:
        requested_items = tuple(
            ScopeItem(
                item_key=item.item_key,
                room_zone_id=item.room_zone_id,
                description=cast(str, item.description),
            )
            for item in command.items
        )
    else:
        requested_items = tuple(
            ScopeItemV2(
                item_key=item.item_key,
                room_zone_id=item.room_zone_id,
                name=cast(str, item.name),
                quantity=cast(int, item.quantity),
                unit=cast(str, item.unit),
                work_note=item.work_note,
                review_status=ScopeItemReviewStatus.CONFIRMED,
                source=ScopeItemSource.CUSTOMER,
            )
            for item in command.items
        )
    try:
        requested_content = await _with_location_condition_snapshot(
            session,
            job_id,
            ScopeContent(
                schema_version=command.scope_schema_version,
                items=requested_items,
                location_conditions=command.location_conditions,
            ),
        )
    except ScopeResourceNotFoundError as error:
        raise AnalysisReviewNotFoundError(job_id) from error
    normalized_content = _normalize_scope_content(requested_content)
    existing = await _child_scope(session, source.id)
    if existing is not None:
        stored_content = ScopeContent.model_validate(existing.content, strict=False)
        if (
            existing.created_by_participant_id != participant_id
            or stored_content != normalized_content
        ):
            raise AnalysisReviewConflictError(source.id)
        await _consume_review_videos(session, row, participant_id)
        return await _to_response(session, row, source, source_result, existing)

    try:
        created = await create_scope_version(
            session,
            job_id,
            participant_id,
            ScopeVersionCreate(
                parent_version_id=source.id,
                content=normalized_content,
            ),
        )
    except ScopeResourceNotFoundError as error:
        raise AnalysisReviewNotFoundError(job_id) from error
    except ScopeVersionConflictError as error:
        raise AnalysisReviewConflictError(source.id) from error
    review = cast(ScopeVersion, await session.get(ScopeVersion, created.id))
    await _consume_review_videos(session, row, participant_id)
    return await _to_response(session, row, source, source_result, review)
