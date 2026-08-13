"""Google Pub/Sub pull subscriber tests without network access."""

from types import SimpleNamespace
from typing import Any

import pytest
from google.api_core import exceptions as google_exceptions

from app.contracts.ports import ProviderError, ProviderErrorKind
from app.platform.event_bus.google_pubsub_subscriber import GooglePubSubPullSubscriber


class StubSubscriber:
    def __init__(self) -> None:
        self.topic = "projects/seqret-test/topics/domain-events"
        self.labels = {"replay_contract": "v1", "seqret_replay_state": "complete"}
        self.received_messages: list[Any] = []
        self.pull_error: Exception | None = None
        self.metadata_error: Exception | None = None
        self.delivery_error: Exception | None = None
        self.calls: list[tuple[str, object, float]] = []
        self.closed = False

    def subscription_path(self, project: str, subscription: str) -> str:
        return f"projects/{project}/subscriptions/{subscription}"

    def get_subscription(self, *, request: object, timeout: float) -> Any:
        self.calls.append(("metadata", request, timeout))
        if self.metadata_error is not None:
            raise self.metadata_error
        return SimpleNamespace(topic=self.topic, labels=self.labels)

    def pull(self, *, request: object, timeout: float) -> Any:
        self.calls.append(("pull", request, timeout))
        if self.pull_error is not None:
            raise self.pull_error
        return SimpleNamespace(received_messages=self.received_messages)

    def acknowledge(self, *, request: object, timeout: float) -> None:
        self.calls.append(("ack", request, timeout))
        if self.delivery_error is not None:
            raise self.delivery_error

    def modify_ack_deadline(self, *, request: object, timeout: float) -> None:
        self.calls.append(("nack", request, timeout))
        if self.delivery_error is not None:
            raise self.delivery_error

    def close(self) -> None:
        self.closed = True


def _adapter(client: StubSubscriber) -> GooglePubSubPullSubscriber:
    return GooglePubSubPullSubscriber(
        "seqret-test",
        "participant-notifications",
        "domain-events",
        client_factory=lambda: client,
    )


@pytest.mark.anyio
async def test_subscriber_pulls_provider_neutral_messages_and_changes_delivery() -> None:
    client = StubSubscriber()
    client.received_messages = [
        SimpleNamespace(
            ack_id="ack-1",
            message=SimpleNamespace(data=b"payload", attributes={"event_id": "event-1"}),
        )
    ]
    adapter = _adapter(client)

    messages = await adapter.pull(max_messages=5, timeout_seconds=2)
    await adapter.acknowledge("ack-1")
    await adapter.nack("ack-2")
    adapter.close()

    assert messages[0].ack_id == "ack-1"
    assert messages[0].data == b"payload"
    assert dict(messages[0].attributes) == {"event_id": "event-1"}
    assert client.calls == [
        (
            "metadata",
            {"subscription": "projects/seqret-test/subscriptions/participant-notifications"},
            10.0,
        ),
        (
            "pull",
            {
                "subscription": "projects/seqret-test/subscriptions/participant-notifications",
                "max_messages": 5,
            },
            2,
        ),
        (
            "ack",
            {
                "subscription": "projects/seqret-test/subscriptions/participant-notifications",
                "ack_ids": ["ack-1"],
            },
            10.0,
        ),
        (
            "nack",
            {
                "subscription": "projects/seqret-test/subscriptions/participant-notifications",
                "ack_ids": ["ack-2"],
                "ack_deadline_seconds": 0,
            },
            10.0,
        ),
    ]
    assert client.closed


@pytest.mark.anyio
async def test_subscriber_treats_empty_pull_deadline_as_no_messages() -> None:
    client = StubSubscriber()
    client.pull_error = google_exceptions.DeadlineExceeded("empty")  # type: ignore[no-untyped-call]

    assert await _adapter(client).pull(max_messages=1, timeout_seconds=0.01) == ()


@pytest.mark.parametrize(
    ("topic", "labels", "message"),
    [
        (
            "projects/seqret-test/topics/other",
            {"replay_contract": "v1", "seqret_replay_state": "complete"},
            "does not match the replay contract",
        ),
        (
            "projects/seqret-test/topics/domain-events",
            {},
            "does not match the replay contract",
        ),
        (
            "projects/seqret-test/topics/domain-events",
            {"replay_contract": "v2", "seqret_replay_state": "complete"},
            "does not match the replay contract",
        ),
        (
            "projects/seqret-test/topics/domain-events",
            {"replay_contract": "v1"},
            "replay state is invalid",
        ),
        (
            "projects/seqret-test/topics/domain-events",
            {"replay_contract": "v1", "seqret_replay_state": "unexpected"},
            "replay state is invalid",
        ),
    ],
)
@pytest.mark.anyio
async def test_subscriber_fails_closed_for_invalid_replay_contract(
    topic: str,
    labels: dict[str, str],
    message: str,
) -> None:
    client = StubSubscriber()
    client.topic = topic
    client.labels = labels

    with pytest.raises(ProviderError, match=message) as error_info:
        await _adapter(client).pull(max_messages=1, timeout_seconds=1)

    assert error_info.value.kind is ProviderErrorKind.INVALID_INPUT
    assert not error_info.value.retryable


@pytest.mark.parametrize("replay_state", ["pending", "initializing"])
@pytest.mark.anyio
async def test_subscriber_treats_known_bootstrap_states_as_noop(
    replay_state: str,
) -> None:
    client = StubSubscriber()
    client.labels["seqret_replay_state"] = replay_state

    assert await _adapter(client).pull(max_messages=1, timeout_seconds=1) == ()
    assert [call[0] for call in client.calls] == ["metadata"]


@pytest.mark.parametrize(
    ("provider_error", "kind", "retryable"),
    [
        (
            google_exceptions.NotFound("missing"),  # type: ignore[no-untyped-call]
            ProviderErrorKind.NOT_FOUND,
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
        (RuntimeError("offline"), ProviderErrorKind.UNAVAILABLE, True),
    ],
)
@pytest.mark.anyio
async def test_subscriber_maps_delivery_errors_without_provider_details(
    provider_error: Exception,
    kind: ProviderErrorKind,
    retryable: bool,
) -> None:
    client = StubSubscriber()
    client.delivery_error = provider_error

    with pytest.raises(ProviderError, match="event subscription acknowledge failed") as error_info:
        await _adapter(client).acknowledge("ack-1")

    assert error_info.value.kind is kind
    assert error_info.value.retryable is retryable


@pytest.mark.anyio
async def test_subscriber_maps_metadata_and_pull_errors() -> None:
    client = StubSubscriber()
    client.metadata_error = RuntimeError("offline")
    with pytest.raises(ProviderError, match="metadata lookup failed"):
        await _adapter(client).pull(max_messages=1, timeout_seconds=1)

    client.metadata_error = None
    client.pull_error = RuntimeError("offline")
    with pytest.raises(ProviderError, match="event subscription pull failed"):
        await _adapter(client).pull(max_messages=1, timeout_seconds=1)


@pytest.mark.parametrize(("max_messages", "timeout"), [(0, 1), (1, 0)])
@pytest.mark.anyio
async def test_subscriber_rejects_nonpositive_pull_limits(
    max_messages: int,
    timeout: float,
) -> None:
    with pytest.raises(ValueError, match="pull limits must be positive"):
        await _adapter(StubSubscriber()).pull(
            max_messages=max_messages,
            timeout_seconds=timeout,
        )


@pytest.mark.anyio
async def test_subscriber_rejects_empty_ack_id() -> None:
    with pytest.raises(ValueError, match="ack_id must not be empty"):
        await _adapter(StubSubscriber()).nack("")


def test_subscriber_uses_default_client_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    client = StubSubscriber()
    monkeypatch.setattr(
        "app.platform.event_bus.google_pubsub_subscriber.pubsub_v1.SubscriberClient",
        lambda: client,
    )

    adapter = GooglePubSubPullSubscriber(
        "seqret-test",
        "participant-notifications",
        "domain-events",
    )

    adapter.close()
    assert client.closed
