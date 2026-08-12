"""Google Pub/Sub EventBus adapter tests without network access."""

import asyncio
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from uuid import uuid4

import pytest
from google.api_core import exceptions as google_exceptions

from app.contracts.events import DomainEvent, DomainEventType
from app.contracts.ports import EventBusPort, ProviderError, ProviderErrorKind
from app.contracts.primitives import AggregateId, EventId, IdempotencyKey
from app.platform.event_bus.google_pubsub import GooglePubSubEventBus


class StubFuture(Future[str]):
    def __init__(self, error: Exception | None = None) -> None:
        super().__init__()
        if error is None:
            self.set_result("message-1")
        else:
            self.set_exception(error)


class StubPublisher:
    def __init__(self, future: Future[str]) -> None:
        self.future = future
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []
        self.stopped = False

    def topic_path(self, project: str, topic: str) -> str:
        return f"projects/{project}/topics/{topic}"

    def publish(self, topic: str, data: bytes, **attrs: str) -> Future[str]:
        self.calls.append((topic, data, attrs))
        return self.future

    def stop(self) -> None:
        self.stopped = True


def _event() -> DomainEvent:
    return DomainEvent(
        event_id=EventId(uuid4()),
        event_type=DomainEventType.SCOPE_LOCKED_V1,
        aggregate_id=AggregateId(uuid4()),
        trace_id="0123456789abcdef0123456789abcdef",
        payload={"scope_version_id": str(uuid4()), "content_hash": "a" * 64},
    )


@pytest.mark.anyio
async def test_pubsub_adapter_serializes_event_and_attributes() -> None:
    future = StubFuture()
    publisher = StubPublisher(future)
    adapter = GooglePubSubEventBus(
        "seqret-test",
        "domain-events",
        client_factory=lambda: publisher,
    )
    event = _event()

    assert isinstance(adapter, EventBusPort)
    await adapter.publish(
        event=event,
        idempotency_key=IdempotencyKey(f"event:{event.event_id}"),
        timeout_seconds=3,
    )

    topic, data, attributes = publisher.calls[0]
    assert topic == "projects/seqret-test/topics/domain-events"
    assert DomainEvent.model_validate_json(data) == event
    assert attributes == {
        "event_id": str(event.event_id),
        "event_type": "scope_locked.v1",
        "schema_version": "1",
        "idempotency_key": f"event:{event.event_id}",
    }
    adapter.close()
    assert publisher.stopped


@pytest.mark.parametrize(
    ("provider_error", "kind", "retryable"),
    [
        (
            google_exceptions.NotFound("missing"),  # type: ignore[no-untyped-call]
            ProviderErrorKind.NOT_FOUND,
            False,
        ),
        (
            google_exceptions.Conflict("conflict"),  # type: ignore[no-untyped-call]
            ProviderErrorKind.CONFLICT,
            False,
        ),
        (
            google_exceptions.BadRequest("bad"),  # type: ignore[no-untyped-call]
            ProviderErrorKind.INVALID_INPUT,
            False,
        ),
        (
            google_exceptions.Forbidden("denied"),  # type: ignore[no-untyped-call]
            ProviderErrorKind.PERMISSION_DENIED,
            False,
        ),
        (
            google_exceptions.DeadlineExceeded("late"),  # type: ignore[no-untyped-call]
            ProviderErrorKind.DEADLINE_EXCEEDED,
            True,
        ),
        (FutureTimeoutError(), ProviderErrorKind.DEADLINE_EXCEEDED, True),
        (RuntimeError("offline"), ProviderErrorKind.UNAVAILABLE, True),
    ],
)
@pytest.mark.anyio
async def test_pubsub_adapter_maps_provider_errors(
    provider_error: Exception,
    kind: ProviderErrorKind,
    retryable: bool,
) -> None:
    adapter = GooglePubSubEventBus(
        "seqret-test",
        "domain-events",
        client_factory=lambda: StubPublisher(StubFuture(provider_error)),
    )

    with pytest.raises(ProviderError, match="event publication failed") as error_info:
        await adapter.publish(
            event=_event(),
            idempotency_key=IdempotencyKey("event:test"),
            timeout_seconds=2,
        )

    assert error_info.value.kind is kind
    assert error_info.value.retryable is retryable


@pytest.mark.anyio
async def test_pubsub_adapter_rejects_nonpositive_timeout() -> None:
    adapter = GooglePubSubEventBus(
        "seqret-test",
        "domain-events",
        client_factory=lambda: StubPublisher(StubFuture()),
    )

    with pytest.raises(ValueError, match="positive"):
        await adapter.publish(
            event=_event(),
            idempotency_key=IdempotencyKey("event:test"),
            timeout_seconds=0,
        )


@pytest.mark.anyio
async def test_pubsub_adapter_times_out_without_blocking_a_worker_thread() -> None:
    adapter = GooglePubSubEventBus(
        "seqret-test",
        "domain-events",
        client_factory=lambda: StubPublisher(Future()),
    )

    with pytest.raises(ProviderError) as error_info:
        await adapter.publish(
            event=_event(),
            idempotency_key=IdempotencyKey("event:test"),
            timeout_seconds=0.01,
        )

    assert error_info.value.kind is ProviderErrorKind.DEADLINE_EXCEEDED
    await asyncio.sleep(0)
