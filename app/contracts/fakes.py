"""Deterministic local fakes for shared port contract tests."""

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from urllib.parse import urlencode

from pydantic import JsonValue

from app.contracts.ai import AnalysisRequest, AnalysisResult
from app.contracts.events import DomainEvent
from app.contracts.ports import (
    ProviderError,
    ProviderErrorKind,
    StorageObjectGeneration,
    StorageObjectMetadata,
    StorageUploadTarget,
)
from app.contracts.primitives import IdempotencyKey, utc_now


class FakeObjectStorage:
    """In-memory private object metadata and operation recorder."""

    def __init__(self) -> None:
        self.metadata: dict[str, StorageObjectMetadata] = {}
        self.deleted_keys: set[str] = set()
        self._delete_requests: dict[IdempotencyKey, tuple[str, str]] = {}

    @staticmethod
    def _require_positive(value: int | float, name: str) -> None:
        if value <= 0:
            msg = f"{name} must be positive"
            raise ValueError(msg)

    @staticmethod
    def _require_generation(value: str) -> None:
        if value != value.strip() or not value or len(value) > 255:
            raise ValueError("generation must be 1..255 non-whitespace characters")

    async def create_upload_url(
        self,
        *,
        object_key: str,
        content_type: str,
        content_length: int,
        expires_in_seconds: int,
        timeout_seconds: float,
    ) -> StorageUploadTarget:
        self._require_positive(content_length, "content_length")
        self._require_positive(expires_in_seconds, "expires_in_seconds")
        self._require_positive(timeout_seconds, "timeout_seconds")
        return StorageUploadTarget(
            url=f"https://storage.invalid/upload/{object_key}",
            headers=(
                ("Content-Type", content_type),
                ("x-goog-if-generation-match", "0"),
            ),
        )

    async def create_read_url(
        self,
        *,
        object_key: str,
        generation: StorageObjectGeneration,
        expires_in_seconds: int,
        timeout_seconds: float,
    ) -> str:
        self._require_generation(generation)
        self._require_positive(expires_in_seconds, "expires_in_seconds")
        self._require_positive(timeout_seconds, "timeout_seconds")
        return f"https://storage.invalid/read/{object_key}?{urlencode({'generation': generation})}"

    async def get_metadata(
        self,
        *,
        object_key: str,
        timeout_seconds: float,
    ) -> StorageObjectMetadata:
        self._require_positive(timeout_seconds, "timeout_seconds")
        try:
            return self.metadata[object_key]
        except KeyError as error:
            raise ProviderError(
                ProviderErrorKind.NOT_FOUND,
                "storage object not found",
                retryable=False,
            ) from error

    async def delete_object(
        self,
        *,
        object_key: str,
        generation: StorageObjectGeneration,
        idempotency_key: IdempotencyKey,
        timeout_seconds: float,
    ) -> None:
        self._require_positive(timeout_seconds, "timeout_seconds")
        self._require_generation(generation)
        request = (object_key, generation)
        previous = self._delete_requests.get(idempotency_key)
        if previous is not None:
            if previous != request:
                raise ProviderError(
                    ProviderErrorKind.CONFLICT,
                    "idempotency key was reused for a different deletion",
                    retryable=False,
                )
            return
        self._delete_requests[idempotency_key] = request
        metadata = self.metadata.get(object_key)
        if metadata is None or metadata.generation != generation:
            return
        del self.metadata[object_key]
        self.deleted_keys.add(object_key)


class FakeTaskQueue:
    """In-memory queue that deduplicates by idempotency key."""

    def __init__(self) -> None:
        self.enqueued: dict[IdempotencyKey, str] = {}
        self.requests: dict[
            IdempotencyKey, tuple[str, str, dict[str, JsonValue], datetime | None]
        ] = {}

    async def enqueue(
        self,
        *,
        queue_name: str,
        handler: str,
        payload: dict[str, JsonValue],
        idempotency_key: IdempotencyKey,
        schedule_at: datetime | None,
        timeout_seconds: float,
    ) -> str:
        if timeout_seconds <= 0:
            msg = "timeout_seconds must be positive"
            raise ValueError(msg)
        request = (queue_name, handler, payload, schedule_at)
        previous = self.requests.get(idempotency_key)
        if previous is not None and previous != request:
            raise ProviderError(
                ProviderErrorKind.CONFLICT,
                "idempotency key was reused for a different task",
                retryable=False,
            )
        task_id = self.enqueued.get(idempotency_key)
        if task_id is None:
            task_id = f"fake-task-{len(self.enqueued) + 1}"
            self.enqueued[idempotency_key] = task_id
            self.requests[idempotency_key] = request
        return task_id


class FakeAIProvider:
    """Delegating fake that caches results by idempotency key."""

    def __init__(
        self,
        result_factory: Callable[[AnalysisRequest], Awaitable[AnalysisResult]],
    ) -> None:
        self.result_factory = result_factory
        self.results: dict[IdempotencyKey, AnalysisResult] = {}
        self.requests: dict[IdempotencyKey, AnalysisRequest] = {}

    async def analyze(
        self,
        *,
        request: AnalysisRequest,
        idempotency_key: IdempotencyKey,
        timeout_seconds: float,
    ) -> AnalysisResult:
        if timeout_seconds <= 0:
            msg = "timeout_seconds must be positive"
            raise ValueError(msg)
        result = self.results.get(idempotency_key)
        if result is None:
            result = await self.result_factory(request)
            self.results[idempotency_key] = result
            self.requests[idempotency_key] = request
        elif self.requests[idempotency_key] != request:
            raise ProviderError(
                ProviderErrorKind.CONFLICT,
                "idempotency key was reused for a different analysis",
                retryable=False,
            )
        return result


class FakeEventBus:
    """In-memory event bus deduplicated by idempotency key."""

    def __init__(self) -> None:
        self.published: dict[IdempotencyKey, DomainEvent] = {}

    async def publish(
        self,
        *,
        event: DomainEvent,
        idempotency_key: IdempotencyKey,
        timeout_seconds: float,
    ) -> None:
        if timeout_seconds <= 0:
            msg = "timeout_seconds must be positive"
            raise ValueError(msg)
        previous = self.published.get(idempotency_key)
        if previous is not None and previous != event:
            raise ProviderError(
                ProviderErrorKind.CONFLICT,
                "idempotency key was reused for a different event",
                retryable=False,
            )
        self.published.setdefault(idempotency_key, event)


class FakeCache:
    """In-memory fixed-window counter with injectable time."""

    def __init__(self, now: Callable[[], datetime] = utc_now) -> None:
        self._now = now
        self._counters: dict[str, tuple[int, datetime]] = {}

    async def increment_fixed_window(
        self,
        *,
        key: str,
        window_seconds: int,
        timeout_seconds: float,
    ) -> int:
        if not key:
            raise ValueError("key must not be empty")
        if window_seconds <= 0 or timeout_seconds <= 0:
            raise ValueError("window_seconds and timeout_seconds must be positive")
        now = self._now()
        current = self._counters.get(key)
        if current is None or current[1] <= now:
            count = 1
            expires_at = now + timedelta(seconds=window_seconds)
        else:
            count = current[0] + 1
            expires_at = current[1]
        self._counters[key] = (count, expires_at)
        return count
