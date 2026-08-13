"""Bounded Google Pub/Sub pull adapter for the notification runtime."""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol, cast

import google.cloud.pubsub_v1 as pubsub_v1  # type: ignore[import-untyped]
from google.api_core import exceptions as google_exceptions

from app.contracts.ports import ProviderError, ProviderErrorKind
from app.modules.notification.consumer import PulledEventMessage

OPERATION_TIMEOUT_SECONDS = 10.0
REPLAY_STATE_LABEL = "seqret_replay_state"
REPLAY_COMPLETE = "complete"
REPLAY_BOOTSTRAP_STATES = frozenset({"pending", "initializing"})
REPLAY_CONTRACT_LABEL = "replay_contract"
REPLAY_CONTRACT_VERSION = "v1"


class PubSubMessage(Protocol):
    data: bytes
    attributes: Mapping[str, str]


class ReceivedMessage(Protocol):
    ack_id: str
    message: PubSubMessage


class PullResponse(Protocol):
    received_messages: Sequence[ReceivedMessage]


class SubscriptionMetadata(Protocol):
    topic: str
    labels: Mapping[str, str]


class SubscriberClient(Protocol):
    def subscription_path(self, project: str, subscription: str) -> str: ...

    def get_subscription(self, *, request: object, timeout: float) -> SubscriptionMetadata: ...

    def pull(self, *, request: object, timeout: float) -> PullResponse: ...

    def acknowledge(self, *, request: object, timeout: float) -> None: ...

    def modify_ack_deadline(self, *, request: object, timeout: float) -> None: ...

    def close(self) -> None: ...


def _create_subscriber_client() -> SubscriberClient:
    return cast(SubscriberClient, pubsub_v1.SubscriberClient())


def _map_subscriber_error(error: Exception, operation: str) -> ProviderError:
    if isinstance(error, google_exceptions.NotFound):
        kind, retryable = ProviderErrorKind.NOT_FOUND, False
    elif isinstance(error, (google_exceptions.BadRequest, google_exceptions.InvalidArgument)):
        kind, retryable = ProviderErrorKind.INVALID_INPUT, False
    elif isinstance(error, (google_exceptions.Forbidden, google_exceptions.Unauthenticated)):
        kind, retryable = ProviderErrorKind.PERMISSION_DENIED, False
    elif isinstance(error, google_exceptions.DeadlineExceeded):
        kind, retryable = ProviderErrorKind.DEADLINE_EXCEEDED, True
    else:
        kind, retryable = ProviderErrorKind.UNAVAILABLE, True
    return ProviderError(kind, f"event subscription {operation} failed", retryable=retryable)


class GooglePubSubPullSubscriber:
    """Pull, ack, and nack one subscription without exposing provider objects."""

    def __init__(
        self,
        project_id: str,
        subscription_id: str,
        topic_id: str,
        *,
        client_factory: Callable[[], SubscriberClient] | None = None,
    ) -> None:
        factory = client_factory or _create_subscriber_client
        self._client = factory()
        self._subscription = self._client.subscription_path(project_id, subscription_id)
        self._topic = f"projects/{project_id}/topics/{topic_id}"

    async def pull(
        self,
        *,
        max_messages: int,
        timeout_seconds: float,
    ) -> tuple[PulledEventMessage, ...]:
        if max_messages <= 0 or timeout_seconds <= 0:
            raise ValueError("pull limits must be positive")
        if not await self._replay_is_ready():
            return ()
        try:
            response = await asyncio.to_thread(
                self._client.pull,
                request={
                    "subscription": self._subscription,
                    "max_messages": max_messages,
                },
                timeout=timeout_seconds,
            )
        except google_exceptions.DeadlineExceeded:
            return ()
        except Exception as error:
            raise _map_subscriber_error(error, "pull") from error
        return tuple(
            PulledEventMessage(
                ack_id=received.ack_id,
                data=received.message.data,
                attributes=dict(received.message.attributes),
            )
            for received in response.received_messages
        )

    async def _replay_is_ready(self) -> bool:
        try:
            metadata = await asyncio.to_thread(
                self._client.get_subscription,
                request={"subscription": self._subscription},
                timeout=OPERATION_TIMEOUT_SECONDS,
            )
        except Exception as error:
            raise _map_subscriber_error(error, "metadata lookup") from error
        if metadata.topic != self._topic or (
            metadata.labels.get(REPLAY_CONTRACT_LABEL) != REPLAY_CONTRACT_VERSION
        ):
            raise ProviderError(
                ProviderErrorKind.INVALID_INPUT,
                "event subscription does not match the replay contract",
                retryable=False,
            )
        replay_state = metadata.labels.get(REPLAY_STATE_LABEL)
        if replay_state == REPLAY_COMPLETE:
            return True
        if replay_state in REPLAY_BOOTSTRAP_STATES:
            return False
        raise ProviderError(
            ProviderErrorKind.INVALID_INPUT,
            "event subscription replay state is invalid",
            retryable=False,
        )

    async def acknowledge(self, ack_id: str) -> None:
        await self._modify_delivery("acknowledge", ack_id)

    async def nack(self, ack_id: str) -> None:
        await self._modify_delivery("nack", ack_id)

    async def _modify_delivery(self, operation: str, ack_id: str) -> None:
        if not ack_id:
            raise ValueError("ack_id must not be empty")
        try:
            if operation == "acknowledge":
                await asyncio.to_thread(
                    self._client.acknowledge,
                    request={"subscription": self._subscription, "ack_ids": [ack_id]},
                    timeout=OPERATION_TIMEOUT_SECONDS,
                )
            else:
                await asyncio.to_thread(
                    self._client.modify_ack_deadline,
                    request={
                        "subscription": self._subscription,
                        "ack_ids": [ack_id],
                        "ack_deadline_seconds": 0,
                    },
                    timeout=OPERATION_TIMEOUT_SECONDS,
                )
        except Exception as error:
            raise _map_subscriber_error(error, operation) from error

    def close(self) -> None:
        self._client.close()
