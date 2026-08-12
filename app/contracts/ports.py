"""Provider-independent asynchronous port interfaces."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Protocol, runtime_checkable

from pydantic import Field, JsonValue, StringConstraints

from app.contracts.ai import AnalysisRequest, AnalysisResult
from app.contracts.events import DomainEvent
from app.contracts.model import ContractModel
from app.contracts.primitives import IdempotencyKey


class StorageObjectMetadata(ContractModel):
    """Metadata verified by a storage adapter after upload."""

    object_key: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    content_type: Annotated[
        str, StringConstraints(min_length=3, max_length=255, pattern=r"^[^/]+/[^/]+$")
    ]
    size_bytes: Annotated[int, Field(ge=0)]
    sha256_hex: (
        Annotated[
            str,
            StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$"),
        ]
        | None
    ) = None
    generation: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
        | None
    ) = None


class ProviderErrorKind(StrEnum):
    """Stable retry classification shared by provider adapters."""

    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    INVALID_INPUT = "invalid_input"
    UNAVAILABLE = "unavailable"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    PERMISSION_DENIED = "permission_denied"


class ProviderError(RuntimeError):
    """Provider-neutral adapter failure without request secrets."""

    def __init__(self, kind: ProviderErrorKind, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


@runtime_checkable
class StoragePort(Protocol):
    """B-owned adapter for private object operations; timeout is seconds."""

    async def create_upload_url(
        self,
        *,
        object_key: str,
        content_type: str,
        content_length: int,
        expires_in_seconds: int,
        timeout_seconds: float,
    ) -> str: ...

    async def create_read_url(
        self,
        *,
        object_key: str,
        generation: str,
        expires_in_seconds: int,
        timeout_seconds: float,
    ) -> str: ...

    async def get_metadata(
        self,
        *,
        object_key: str,
        timeout_seconds: float,
    ) -> StorageObjectMetadata: ...

    async def delete_object(
        self,
        *,
        object_key: str,
        generation: str | None,
        idempotency_key: IdempotencyKey,
        timeout_seconds: float,
    ) -> None: ...


ObjectStoragePort = StoragePort


@runtime_checkable
class TaskQueuePort(Protocol):
    """B-owned task adapter; duplicate idempotency keys must not enqueue twice."""

    async def enqueue(
        self,
        *,
        queue_name: str,
        handler: str,
        payload: dict[str, JsonValue],
        idempotency_key: IdempotencyKey,
        schedule_at: datetime | None,
        timeout_seconds: float,
    ) -> str: ...


@runtime_checkable
class AIProviderPort(Protocol):
    """B-owned AI adapter returning only the versioned draft contract."""

    async def analyze(
        self,
        *,
        request: AnalysisRequest,
        idempotency_key: IdempotencyKey,
        timeout_seconds: float,
    ) -> AnalysisResult: ...


@runtime_checkable
class EventBusPort(Protocol):
    """A-owned event adapter; event_id is the consumer deduplication key."""

    async def publish(
        self,
        *,
        event: DomainEvent,
        idempotency_key: IdempotencyKey,
        timeout_seconds: float,
    ) -> None: ...


@runtime_checkable
class CachePort(Protocol):
    """A-owned atomic counter cache; existing windows must keep their original TTL."""

    async def increment_fixed_window(
        self,
        *,
        key: str,
        window_seconds: int,
        timeout_seconds: float,
    ) -> int: ...
