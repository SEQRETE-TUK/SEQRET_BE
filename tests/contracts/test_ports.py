"""Contract tests reused by deterministic local port fakes."""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue

from app.contracts import (
    AIProviderPort,
    AnalysisCarryDistanceCondition,
    AnalysisFloorCondition,
    AnalysisRequest,
    AnalysisResult,
    AnalysisRunId,
    AnalysisSourceContext,
    CachePort,
    CaptureSessionId,
    DomainEvent,
    DomainEventType,
    DraftItem,
    DraftLocationCondition,
    EventBusPort,
    EventId,
    IdempotencyKey,
    MediaAssetId,
    ObjectStoragePort,
    ProviderError,
    ProviderErrorKind,
    StorageObjectMetadata,
    StorageUploadTarget,
    TaskQueuePort,
)
from app.contracts.fakes import (
    FakeAIProvider,
    FakeCache,
    FakeEventBus,
    FakeObjectStorage,
    FakeTaskQueue,
)
from app.contracts.primitives import AggregateId


@pytest.fixture
def anyio_backend() -> str:
    """Run async contract tests with asyncio only."""

    return "asyncio"


@pytest.mark.anyio
async def test_storage_fake_satisfies_protocol_and_deduplicates_deletion() -> None:
    storage = FakeObjectStorage()
    key = IdempotencyKey("media-delete:1")
    storage.metadata["jobs/1/photo.jpg"] = StorageObjectMetadata(
        object_key="jobs/1/photo.jpg",
        content_type="image/jpeg",
        size_bytes=10,
        generation="7",
    )
    storage.contents["jobs/1/photo.jpg"] = b"verified-media"

    assert isinstance(storage, ObjectStoragePort)
    upload = await storage.create_upload_url(
        object_key="jobs/1/photo.jpg",
        content_type="image/jpeg",
        content_length=10,
        expires_in_seconds=300,
        timeout_seconds=2,
    )
    assert upload.url == "https://storage.invalid/upload/jobs/1/photo.jpg"
    assert upload.headers == (
        ("Content-Type", "image/jpeg"),
        ("x-goog-if-generation-match", "0"),
    )
    assert (
        await storage.create_read_url(
            object_key="jobs/1/photo.jpg",
            generation="7&latest=false",
            expires_in_seconds=60,
            timeout_seconds=2,
        )
        == "https://storage.invalid/read/jobs/1/photo.jpg?generation=7%26latest%3Dfalse"
    )
    assert (
        await storage.get_metadata(object_key="jobs/1/photo.jpg", timeout_seconds=2)
    ).size_bytes == 10
    assert (
        await storage.calculate_sha256(
            object_key="jobs/1/photo.jpg",
            generation="7",
            timeout_seconds=2,
        )
        == hashlib.sha256(b"verified-media").hexdigest()
    )

    await storage.delete_object(
        object_key="jobs/1/photo.jpg",
        generation="7",
        idempotency_key=key,
        timeout_seconds=2,
    )
    await storage.delete_object(
        object_key="jobs/1/photo.jpg",
        generation="7",
        idempotency_key=key,
        timeout_seconds=2,
    )

    assert storage.deleted_keys == {"jobs/1/photo.jpg"}
    assert "jobs/1/photo.jpg" not in storage.metadata


@pytest.mark.anyio
async def test_storage_fake_preserves_a_different_generation_and_accepts_missing_snapshot() -> None:
    storage = FakeObjectStorage()
    current = StorageObjectMetadata(
        object_key="jobs/1/photo.jpg",
        content_type="image/jpeg",
        size_bytes=10,
        generation="8",
    )
    storage.metadata[current.object_key] = current

    with pytest.raises(ProviderError) as stale_generation:
        await storage.calculate_sha256(
            object_key=current.object_key,
            generation="7",
            timeout_seconds=2,
        )
    with pytest.raises(ProviderError) as missing_content:
        await storage.calculate_sha256(
            object_key=current.object_key,
            generation="8",
            timeout_seconds=2,
        )

    await storage.delete_object(
        object_key=current.object_key,
        generation="7",
        idempotency_key=IdempotencyKey("media-delete:old-generation"),
        timeout_seconds=2,
    )
    await storage.delete_object(
        object_key="jobs/1/missing.jpg",
        generation="7",
        idempotency_key=IdempotencyKey("media-delete:missing"),
        timeout_seconds=2,
    )

    assert storage.metadata[current.object_key] is current
    assert storage.deleted_keys == set()
    assert stale_generation.value.kind is ProviderErrorKind.NOT_FOUND
    assert missing_content.value.kind is ProviderErrorKind.NOT_FOUND


def test_storage_upload_target_preserves_provider_opaque_values() -> None:
    url = "  https://storage.invalid/upload/%2F?X-Signature=A%2B  "
    headers = (("If-None-Match", "*"), ("X-Signed-Exact", "  keep both spaces  "))

    target = StorageUploadTarget(url=url, headers=headers)

    assert target.url == url
    assert target.headers == headers
    assert "X-Signature" not in repr(target)


def test_storage_upload_target_rejects_invalid_header_names() -> None:
    cases: tuple[tuple[tuple[str, str], ...], ...] = (
        (("", "x"),),
        (("X-Signed", "1"), ("x-signed", "2")),
    )
    for headers in cases:
        with pytest.raises(ValueError, match="nonempty and unique"):
            StorageUploadTarget(
                url="https://storage.invalid/upload",
                headers=headers,
            )


def test_storage_metadata_rejects_invalid_generation() -> None:
    for generation in ("", " ", "x" * 256):
        with pytest.raises(ValueError):
            StorageObjectMetadata(
                object_key="jobs/1/photo.jpg",
                content_type="image/jpeg",
                size_bytes=10,
                generation=generation,
            )


@pytest.mark.anyio
async def test_task_queue_fake_returns_one_task_per_idempotency_key() -> None:
    queue = FakeTaskQueue()
    key = IdempotencyKey("analysis:1")

    assert isinstance(queue, TaskQueuePort)
    first = await queue.enqueue(
        queue_name="analysis",
        handler="analyze_capture",
        payload={"capture_session_id": "same-capture"},
        idempotency_key=key,
        schedule_at=None,
        timeout_seconds=2,
    )
    second = await queue.enqueue(
        queue_name="analysis",
        handler="analyze_capture",
        payload={"capture_session_id": "same-capture"},
        idempotency_key=key,
        schedule_at=None,
        timeout_seconds=2,
    )

    assert first == second
    assert len(queue.enqueued) == 1


@pytest.mark.anyio
async def test_ai_fake_returns_versioned_result_once() -> None:
    calls = 0

    async def result_factory(request: AnalysisRequest) -> AnalysisResult:
        nonlocal calls
        calls += 1
        return AnalysisResult(
            analysis_run_id=request.analysis_run_id,
            capture_session_id=request.capture_session_id,
            model_name="fake-model",
            model_version="1",
            prompt_version="1",
            draft_items=(),
        )

    provider = FakeAIProvider(result_factory)
    run_id = AnalysisRunId(uuid4())
    key = IdempotencyKey("analysis-result:1")
    request = AnalysisRequest(
        analysis_run_id=run_id,
        capture_session_id=CaptureSessionId(uuid4()),
        source_media_asset_ids=(MediaAssetId(uuid4()),),
        object_keys=("jobs/1/photo.jpg",),
        content_types=("image/jpeg",),
        model_name="fake-model",
        model_version="1",
        prompt_version="1",
    )

    assert isinstance(provider, AIProviderPort)
    first = await provider.analyze(
        request=request,
        idempotency_key=key,
        timeout_seconds=30,
    )
    second = await provider.analyze(
        request=request,
        idempotency_key=key,
        timeout_seconds=30,
    )

    assert first is second
    assert calls == 1

    with pytest.raises(ProviderError, match="different analysis"):
        await provider.analyze(
            request=request.model_copy(update={"object_keys": ("jobs/1/other.jpg",)}),
            idempotency_key=key,
            timeout_seconds=30,
        )


def _analysis_source_context(media_asset_id: MediaAssetId) -> AnalysisSourceContext:
    return AnalysisSourceContext(
        media_asset_id=media_asset_id,
        location_id=uuid4(),
        location_kind="origin",
        room_zone_id=uuid4(),
    )


def _analysis_v2_item(
    media_asset_id: MediaAssetId,
    *,
    item_key: str = "box",
    quantity: int | None = 4,
    unit: str | None = "개",
) -> DraftItem:
    return DraftItem(
        item_key=item_key,
        description="이삿짐 상자 4개",
        name="이삿짐 상자",
        quantity=quantity,
        unit=unit,
        work_note="완충 포장",
        confidence=0.91,
        source_media_asset_ids=(media_asset_id,),
    )


def _analysis_v2_location(
    media_asset_id: MediaAssetId,
    *,
    location_id: UUID | None = None,
    location_kind: str = "origin",
    source_media_asset_ids: tuple[MediaAssetId, ...] | None = None,
) -> DraftLocationCondition:
    return DraftLocationCondition(
        location_id=location_id or uuid4(),
        location_kind=location_kind,  # type: ignore[arg-type]
        residence_type="studio",
        floor=AnalysisFloorCondition(status="known", value=3),
        elevator="available",
        stairs="not_required",
        parking_access="restricted",
        carry_distance=AnalysisCarryDistanceCondition(status="known", value_m=35),
        access_note="골목 진입 확인 필요",
        confidence=0.74,
        review_required_fields=("parking_access",),
        source_media_asset_ids=(media_asset_id,)
        if source_media_asset_ids is None
        else source_media_asset_ids,
    )


def _analysis_v2_result(
    media_asset_id: MediaAssetId,
    *,
    draft_items: tuple[DraftItem, ...] | None = None,
    review_required_items: tuple[DraftItem, ...] = (),
    location_condition_suggestions: tuple[DraftLocationCondition, ...] | None = None,
) -> AnalysisResult:
    return AnalysisResult(
        analysis_run_id=AnalysisRunId(uuid4()),
        capture_session_id=CaptureSessionId(uuid4()),
        model_name="fake-model",
        model_version="2",
        prompt_version="scope-v2",
        result_schema_version=2,
        draft_items=((_analysis_v2_item(media_asset_id),) if draft_items is None else draft_items),
        review_required_items=review_required_items,
        location_condition_suggestions=(
            (_analysis_v2_location(media_asset_id),)
            if location_condition_suggestions is None
            else location_condition_suggestions
        ),
    )


def test_analysis_v2_contract_preserves_structured_items_and_location_risks() -> None:
    media_asset_id = MediaAssetId(uuid4())
    context = _analysis_source_context(media_asset_id)
    request = AnalysisRequest(
        analysis_run_id=AnalysisRunId(uuid4()),
        capture_session_id=CaptureSessionId(uuid4()),
        source_media_asset_ids=(media_asset_id,),
        object_keys=("jobs/1/opaque-object",),
        content_types=("video/mp4",),
        model_name="fake-model",
        model_version="2",
        prompt_version="scope-v2",
        requested_result_schema_version=2,
        source_contexts=(context,),
    )
    review_item = _analysis_v2_item(
        media_asset_id,
        item_key="air-conditioner",
        quantity=None,
        unit=None,
    )
    result = _analysis_v2_result(
        media_asset_id,
        review_required_items=(review_item,),
    )

    assert request.source_contexts == (context,)
    assert result.draft_items[0].quantity == 4
    assert result.review_required_items[0].quantity is None
    assert result.location_condition_suggestions[0].carry_distance.value_m == 35


def test_analysis_request_v2_requires_matching_source_contexts() -> None:
    media_asset_id = MediaAssetId(uuid4())
    base = {
        "analysis_run_id": AnalysisRunId(uuid4()),
        "capture_session_id": CaptureSessionId(uuid4()),
        "source_media_asset_ids": (media_asset_id,),
        "object_keys": ("jobs/1/opaque-object",),
        "content_types": ("image/jpeg",),
        "model_name": "fake-model",
        "model_version": "2",
        "prompt_version": "scope-v2",
        "requested_result_schema_version": 2,
    }
    with pytest.raises(ValueError, match="requires source contexts"):
        AnalysisRequest.model_validate(base)
    with pytest.raises(ValueError, match="must match source media asset IDs in order"):
        AnalysisRequest.model_validate(
            {
                **base,
                "source_contexts": (_analysis_source_context(MediaAssetId(uuid4())),),
            }
        )


def test_analysis_location_numeric_conditions_require_explicit_knowledge() -> None:
    cases: tuple[tuple[str, dict[str, object], str], ...] = (
        ("floor", {"status": "known", "value": None}, "known floor requires a value"),
        (
            "floor",
            {"status": "unknown", "value": 3},
            "unknown floor forbids one",
        ),
        (
            "carry_distance",
            {"status": "known", "value_m": None},
            "known carry distance requires a value",
        ),
        (
            "carry_distance",
            {"status": "unknown", "value_m": 35},
            "unknown distance forbids one",
        ),
    )
    for field, value, message in cases:
        condition_type = (
            AnalysisFloorCondition if field == "floor" else AnalysisCarryDistanceCondition
        )
        with pytest.raises(ValueError, match=message):
            condition_type.model_validate(value)


def test_analysis_location_condition_rejects_duplicate_review_fields_and_sources() -> None:
    media_asset_id = MediaAssetId(uuid4())
    base = _analysis_v2_location(media_asset_id).model_dump()
    with pytest.raises(ValueError, match="review-required fields must be unique"):
        DraftLocationCondition.model_validate(
            {**base, "review_required_fields": ("floor", "floor")}
        )
    with pytest.raises(ValueError, match="source media asset IDs must be unique"):
        DraftLocationCondition.model_validate(
            {**base, "source_media_asset_ids": (media_asset_id, media_asset_id)}
        )


def test_analysis_result_v1_rejects_v2_only_fields() -> None:
    media_asset_id = MediaAssetId(uuid4())
    with pytest.raises(ValueError, match="cannot contain structured item fields"):
        AnalysisResult(
            analysis_run_id=AnalysisRunId(uuid4()),
            capture_session_id=CaptureSessionId(uuid4()),
            model_name="fake-model",
            model_version="1",
            prompt_version="scope-v1",
            draft_items=(_analysis_v2_item(media_asset_id),),
        )
    with pytest.raises(ValueError, match="cannot contain location conditions"):
        AnalysisResult(
            analysis_run_id=AnalysisRunId(uuid4()),
            capture_session_id=CaptureSessionId(uuid4()),
            model_name="fake-model",
            model_version="1",
            prompt_version="scope-v1",
            draft_items=(),
            location_condition_suggestions=(_analysis_v2_location(media_asset_id),),
        )


def test_analysis_result_v2_rejects_invalid_item_shapes() -> None:
    cases: tuple[tuple[tuple[DraftItem, ...], tuple[DraftItem, ...], str], ...] = (
        (
            (
                DraftItem(
                    item_key="box",
                    description="상자",
                    confidence=0.8,
                    source_media_asset_ids=(MediaAssetId(UUID(int=1)),),
                ),
            ),
            (),
            "items require name",
        ),
        (
            (_analysis_v2_item(MediaAssetId(UUID(int=1)), quantity=None, unit=None),),
            (),
            "draft items require quantity and unit",
        ),
        (
            (),
            (_analysis_v2_item(MediaAssetId(UUID(int=1)), quantity=1, unit=None),),
            "quantity and unit must be present together",
        ),
        (
            (_analysis_v2_item(MediaAssetId(UUID(int=1))),),
            (_analysis_v2_item(MediaAssetId(UUID(int=2)), item_key="box"),),
            "item keys must be unique",
        ),
        (
            (
                _analysis_v2_item(MediaAssetId(UUID(int=1))).model_copy(
                    update={"source_media_asset_ids": ()}
                ),
            ),
            (),
            "require unique source media asset IDs",
        ),
        (
            (
                _analysis_v2_item(MediaAssetId(UUID(int=1))).model_copy(
                    update={
                        "source_media_asset_ids": (
                            MediaAssetId(UUID(int=1)),
                            MediaAssetId(UUID(int=1)),
                        )
                    }
                ),
            ),
            (),
            "require unique source media asset IDs",
        ),
    )
    for draft_items, review_items, message in cases:
        with pytest.raises(ValueError, match=message):
            _analysis_v2_result(
                MediaAssetId(UUID(int=9)),
                draft_items=draft_items,
                review_required_items=review_items,
                location_condition_suggestions=(),
            )


def test_analysis_result_v2_rejects_invalid_location_suggestions() -> None:
    media_asset_id = MediaAssetId(uuid4())
    location_id = uuid4()
    origin = _analysis_v2_location(
        media_asset_id,
        location_id=location_id,
        location_kind="origin",
    )
    with pytest.raises(ValueError, match="location suggestions must be unique"):
        _analysis_v2_result(
            media_asset_id,
            location_condition_suggestions=(
                origin,
                _analysis_v2_location(
                    media_asset_id,
                    location_id=location_id,
                    location_kind="destination",
                ),
            ),
        )
    with pytest.raises(ValueError, match="location suggestions must be unique"):
        _analysis_v2_result(
            media_asset_id,
            location_condition_suggestions=(
                origin,
                _analysis_v2_location(media_asset_id, location_kind="origin"),
            ),
        )
    with pytest.raises(ValueError, match="require source media asset IDs"):
        _analysis_v2_result(
            media_asset_id,
            location_condition_suggestions=(
                _analysis_v2_location(media_asset_id, source_media_asset_ids=()),
            ),
        )


def test_analysis_request_requires_unique_one_to_one_sources() -> None:
    cases: tuple[tuple[tuple[MediaAssetId, ...], tuple[str, ...], str], ...] = (
        ((MediaAssetId(uuid4()),), ("first", "second"), "same length"),
        (
            (MediaAssetId(UUID(int=1)), MediaAssetId(UUID(int=1))),
            ("first", "second"),
            "asset IDs must be unique",
        ),
        (
            (MediaAssetId(UUID(int=1)), MediaAssetId(UUID(int=2))),
            ("same", "same"),
            "object keys must be unique",
        ),
    )
    for source_ids, object_keys, message in cases:
        with pytest.raises(ValueError, match=message):
            AnalysisRequest(
                analysis_run_id=AnalysisRunId(uuid4()),
                capture_session_id=CaptureSessionId(uuid4()),
                source_media_asset_ids=source_ids,
                object_keys=object_keys,
                content_types=tuple("image/jpeg" for _ in source_ids),
                model_name="fake-model",
                model_version="1",
                prompt_version="1",
            )


def test_analysis_request_rejects_unsupported_content_type() -> None:
    with pytest.raises(ValueError, match="image/jpeg"):
        AnalysisRequest(
            analysis_run_id=AnalysisRunId(uuid4()),
            capture_session_id=CaptureSessionId(uuid4()),
            source_media_asset_ids=(MediaAssetId(uuid4()),),
            object_keys=("jobs/1/opaque-object",),
            content_types=("application/octet-stream",),  # type: ignore[arg-type]
            model_name="fake-model",
            model_version="1",
            prompt_version="1",
        )


def _analysis_completed_payload() -> dict[str, JsonValue]:
    return {
        "capture_session_id": str(uuid4()),
        "analysis_run_id": str(uuid4()),
        "scope_version_id": str(uuid4()),
    }


@pytest.mark.anyio
async def test_event_bus_fake_keeps_first_event_for_idempotency_key() -> None:
    event_bus = FakeEventBus()
    key = IdempotencyKey("event:1")
    first = DomainEvent(
        event_id=EventId(uuid4()),
        event_type=DomainEventType.ANALYSIS_COMPLETED_V1,
        aggregate_id=AggregateId(uuid4()),
        trace_id="0123456789abcdef0123456789abcdef",
        payload=_analysis_completed_payload(),
    )
    assert isinstance(event_bus, EventBusPort)
    await event_bus.publish(event=first, idempotency_key=key, timeout_seconds=2)
    await event_bus.publish(event=first, idempotency_key=key, timeout_seconds=2)

    assert event_bus.published[key] is first


@pytest.mark.anyio
async def test_cache_fake_keeps_fixed_window_expiry() -> None:
    current = datetime(2026, 8, 12, 7, 0, tzinfo=UTC)
    cache = FakeCache(lambda: current)

    assert isinstance(cache, CachePort)
    assert (
        await cache.increment_fixed_window(
            key="rate:token-hash",
            window_seconds=60,
            timeout_seconds=1,
        )
        == 1
    )
    current += timedelta(seconds=59)
    assert (
        await cache.increment_fixed_window(
            key="rate:token-hash",
            window_seconds=60,
            timeout_seconds=1,
        )
        == 2
    )
    current += timedelta(seconds=1)
    assert (
        await cache.increment_fixed_window(
            key="rate:token-hash",
            window_seconds=60,
            timeout_seconds=1,
        )
        == 1
    )


@pytest.mark.anyio
async def test_fakes_reject_idempotency_key_reuse_for_different_requests() -> None:
    queue = FakeTaskQueue()
    event_bus = FakeEventBus()
    storage = FakeObjectStorage()
    task_key = IdempotencyKey("task:conflict")
    event_key = IdempotencyKey("event:conflict")
    delete_key = IdempotencyKey("delete:conflict")
    event = DomainEvent(
        event_id=EventId(uuid4()),
        event_type=DomainEventType.ANALYSIS_COMPLETED_V1,
        aggregate_id=AggregateId(uuid4()),
        trace_id="0123456789abcdef0123456789abcdef",
        payload=_analysis_completed_payload(),
    )

    await queue.enqueue(
        queue_name="analysis",
        handler="first",
        payload={},
        idempotency_key=task_key,
        schedule_at=None,
        timeout_seconds=2,
    )
    await event_bus.publish(event=event, idempotency_key=event_key, timeout_seconds=2)
    await storage.delete_object(
        object_key="first",
        generation="1",
        idempotency_key=delete_key,
        timeout_seconds=2,
    )

    with pytest.raises(ProviderError, match="different task"):
        await queue.enqueue(
            queue_name="analysis",
            handler="second",
            payload={},
            idempotency_key=task_key,
            schedule_at=None,
            timeout_seconds=2,
        )
    with pytest.raises(ProviderError, match="different event"):
        await event_bus.publish(
            event=event.model_copy(update={"event_id": EventId(uuid4())}),
            idempotency_key=event_key,
            timeout_seconds=2,
        )
    with pytest.raises(ProviderError, match="different deletion"):
        await storage.delete_object(
            object_key="second",
            generation="1",
            idempotency_key=delete_key,
            timeout_seconds=2,
        )


@pytest.mark.anyio
async def test_storage_fake_maps_missing_object_to_provider_error() -> None:
    storage = FakeObjectStorage()

    with pytest.raises(ProviderError) as error_info:
        await storage.get_metadata(object_key="missing", timeout_seconds=2)

    assert error_info.value.kind is ProviderErrorKind.NOT_FOUND
    assert error_info.value.retryable is False


@pytest.mark.anyio
async def test_fakes_reject_nonpositive_timeouts_and_lengths() -> None:
    storage = FakeObjectStorage()
    queue = FakeTaskQueue()
    event_bus = FakeEventBus()
    cache = FakeCache()
    event = DomainEvent(
        event_id=EventId(uuid4()),
        event_type=DomainEventType.ANALYSIS_COMPLETED_V1,
        aggregate_id=AggregateId(uuid4()),
        trace_id="0123456789abcdef0123456789abcdef",
        payload=_analysis_completed_payload(),
    )

    async def result_factory(request: AnalysisRequest) -> AnalysisResult:
        return AnalysisResult(
            analysis_run_id=request.analysis_run_id,
            capture_session_id=request.capture_session_id,
            model_name="fake-model",
            model_version="1",
            prompt_version="1",
            draft_items=(),
        )

    provider = FakeAIProvider(result_factory)

    with pytest.raises(ValueError, match="content_length must be positive"):
        await storage.create_upload_url(
            object_key="object",
            content_type="image/jpeg",
            content_length=0,
            expires_in_seconds=60,
            timeout_seconds=2,
        )
    with pytest.raises(ValueError, match="expires_in_seconds must be positive"):
        await storage.create_upload_url(
            object_key="object",
            content_type="image/jpeg",
            content_length=1,
            expires_in_seconds=0,
            timeout_seconds=2,
        )
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        await storage.create_upload_url(
            object_key="object",
            content_type="image/jpeg",
            content_length=1,
            expires_in_seconds=60,
            timeout_seconds=0,
        )
    with pytest.raises(ValueError, match="expires_in_seconds must be positive"):
        await storage.create_read_url(
            object_key="object",
            generation="7",
            expires_in_seconds=0,
            timeout_seconds=2,
        )
    with pytest.raises(ValueError, match="generation must be"):
        await storage.create_read_url(
            object_key="object",
            generation="",
            expires_in_seconds=60,
            timeout_seconds=2,
        )
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        await storage.get_metadata(object_key="object", timeout_seconds=0)
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        await storage.delete_object(
            object_key="object",
            generation="1",
            idempotency_key=IdempotencyKey("delete:timeout"),
            timeout_seconds=0,
        )
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        await queue.enqueue(
            queue_name="analysis",
            handler="handler",
            payload={},
            idempotency_key=IdempotencyKey("task:timeout"),
            schedule_at=None,
            timeout_seconds=0,
        )
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        await provider.analyze(
            request=AnalysisRequest(
                analysis_run_id=AnalysisRunId(uuid4()),
                capture_session_id=CaptureSessionId(uuid4()),
                source_media_asset_ids=(MediaAssetId(uuid4()),),
                object_keys=("object",),
                content_types=("image/jpeg",),
                model_name="fake-model",
                model_version="1",
                prompt_version="1",
            ),
            idempotency_key=IdempotencyKey("analysis:timeout"),
            timeout_seconds=0,
        )
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        await event_bus.publish(
            event=event,
            idempotency_key=IdempotencyKey("event:timeout"),
            timeout_seconds=0,
        )
    with pytest.raises(ValueError, match="key must not be empty"):
        await cache.increment_fixed_window(key="", window_seconds=60, timeout_seconds=1)
    with pytest.raises(ValueError, match="must be positive"):
        await cache.increment_fixed_window(key="rate:key", window_seconds=0, timeout_seconds=1)
    with pytest.raises(ValueError, match="must be positive"):
        await cache.increment_fixed_window(key="rate:key", window_seconds=60, timeout_seconds=0)


def test_provider_error_carries_stable_retry_classification() -> None:
    error = ProviderError(
        ProviderErrorKind.UNAVAILABLE,
        "storage temporarily unavailable",
        retryable=True,
    )

    assert error.kind is ProviderErrorKind.UNAVAILABLE
    assert error.retryable is True
    assert str(error) == "storage temporarily unavailable"
