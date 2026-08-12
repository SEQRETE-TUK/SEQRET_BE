"""Google Pub/Sub adapter isolated behind the provider-neutral event port."""

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Protocol, cast

from google.api_core import exceptions as google_exceptions
from google.cloud import pubsub_v1  # type: ignore[import-untyped]

from app.contracts.events import DomainEvent
from app.contracts.ports import ProviderError, ProviderErrorKind
from app.contracts.primitives import IdempotencyKey


class PublisherClient(Protocol):
    def topic_path(self, project: str, topic: str) -> str: ...

    def publish(self, topic: str, data: bytes, **attrs: str) -> Future[str]: ...

    def stop(self) -> None: ...


def _map_publish_error(error: Exception) -> ProviderError:
    if isinstance(error, google_exceptions.NotFound):
        kind, retryable = ProviderErrorKind.NOT_FOUND, False
    elif isinstance(error, (google_exceptions.Conflict, google_exceptions.AlreadyExists)):
        kind, retryable = ProviderErrorKind.CONFLICT, False
    elif isinstance(error, (google_exceptions.BadRequest, google_exceptions.InvalidArgument)):
        kind, retryable = ProviderErrorKind.INVALID_INPUT, False
    elif isinstance(error, (google_exceptions.Forbidden, google_exceptions.Unauthenticated)):
        kind, retryable = ProviderErrorKind.PERMISSION_DENIED, False
    elif isinstance(error, (google_exceptions.DeadlineExceeded, FutureTimeoutError)):
        kind, retryable = ProviderErrorKind.DEADLINE_EXCEEDED, True
    else:
        kind, retryable = ProviderErrorKind.UNAVAILABLE, True
    return ProviderError(kind, "event publication failed", retryable=retryable)


class GooglePubSubEventBus:
    """Publish strict event JSON while retaining event_id for consumer deduplication."""

    def __init__(
        self,
        project_id: str,
        topic_id: str,
        *,
        client_factory: Callable[[], PublisherClient] | None = None,
    ) -> None:
        factory = client_factory or (lambda: cast(PublisherClient, pubsub_v1.PublisherClient()))
        self._client = factory()
        self._topic = self._client.topic_path(project_id, topic_id)

    async def publish(
        self,
        *,
        event: DomainEvent,
        idempotency_key: IdempotencyKey,
        timeout_seconds: float,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        try:
            future = self._client.publish(
                self._topic,
                event.model_dump_json().encode("utf-8"),
                event_id=str(event.event_id),
                event_type=event.event_type.value,
                schema_version=str(event.schema_version),
                idempotency_key=idempotency_key,
            )
            await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout_seconds)
        except Exception as error:
            raise _map_publish_error(error) from error

    def close(self) -> None:
        """Flush the publisher during runtime shutdown."""

        self._client.stop()
