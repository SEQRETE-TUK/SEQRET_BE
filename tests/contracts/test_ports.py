"""Contract tests reused by deterministic local port fakes."""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.contracts import (
    AIProviderPort,
    AnalysisRequest,
    AnalysisResult,
    AnalysisRunId,
    CachePort,
    CaptureSessionId,
    DomainEvent,
    DomainEventType,
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


@pytest.mark.parametrize("headers", [(("", "x"),), (("X-Signed", "1"), ("x-signed", "2"))])
def test_storage_upload_target_rejects_invalid_header_names(
    headers: tuple[tuple[str, str], ...],
) -> None:
    with pytest.raises(ValueError, match="nonempty and unique"):
        StorageUploadTarget(
            url="https://storage.invalid/upload",
            headers=headers,
        )


@pytest.mark.parametrize("generation", ["", " ", "x" * 256])
def test_storage_metadata_rejects_invalid_generation(generation: str) -> None:
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


@pytest.mark.parametrize(
    ("source_ids", "object_keys", "message"),
    [
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
    ],
)
def test_analysis_request_requires_unique_one_to_one_sources(
    source_ids: tuple[MediaAssetId, ...],
    object_keys: tuple[str, ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        AnalysisRequest(
            analysis_run_id=AnalysisRunId(uuid4()),
            capture_session_id=CaptureSessionId(uuid4()),
            source_media_asset_ids=source_ids,
            object_keys=object_keys,
            model_name="fake-model",
            model_version="1",
            prompt_version="1",
        )


@pytest.mark.anyio
async def test_event_bus_fake_keeps_first_event_for_idempotency_key() -> None:
    event_bus = FakeEventBus()
    key = IdempotencyKey("event:1")
    first = DomainEvent(
        event_id=EventId(uuid4()),
        event_type=DomainEventType.ANALYSIS_COMPLETED_V1,
        aggregate_id=AggregateId(uuid4()),
        trace_id="0123456789abcdef0123456789abcdef",
        payload={},
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
        payload={},
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
        payload={},
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
