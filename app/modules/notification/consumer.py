"""One-batch notification event consumption with commit-before-ack semantics."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.events import DomainEvent
from app.contracts.ports import ProviderError
from app.modules.notification.service import consume_notification_event
from app.platform.db import transactional_session


@dataclass(frozen=True, slots=True)
class PulledEventMessage:
    """Provider-neutral Pub/Sub delivery fields needed by the consumer."""

    ack_id: str
    data: bytes
    attributes: Mapping[str, str]


class NotificationSubscriber(Protocol):
    """Private runtime boundary for one pull delivery batch."""

    async def pull(
        self,
        *,
        max_messages: int,
        timeout_seconds: float,
    ) -> tuple[PulledEventMessage, ...]: ...

    async def acknowledge(self, ack_id: str) -> None: ...

    async def nack(self, ack_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class NotificationConsumerResult:
    pulled: int
    acknowledged: int
    failed: int


def parse_domain_event(message: PulledEventMessage) -> DomainEvent:
    """Reject unknown JSON fields and mismatched Pub/Sub routing attributes."""

    event = DomainEvent.model_validate_json(message.data)
    expected_attributes = {
        "event_id": str(event.event_id),
        "event_type": event.event_type.value,
        "schema_version": str(event.schema_version),
        "idempotency_key": f"event:{event.event_id}",
    }
    if message.attributes != expected_attributes:
        raise ValueError("Pub/Sub attributes do not match the DomainEvent envelope")
    return event


async def consume_notification_events_once(
    session_factory: async_sessionmaker[AsyncSession],
    subscriber: NotificationSubscriber,
    *,
    batch_size: int,
    pull_timeout_seconds: float,
) -> NotificationConsumerResult:
    """Consume one bounded batch; a message is acknowledged only after DB commit."""

    messages = await subscriber.pull(
        max_messages=batch_size,
        timeout_seconds=pull_timeout_seconds,
    )
    acknowledged = failed = 0
    for message in messages:
        try:
            event = parse_domain_event(message)
            async with transactional_session(session_factory) as session:
                await consume_notification_event(session, event)
            await subscriber.acknowledge(message.ack_id)
            acknowledged += 1
        except Exception:
            failed += 1
            try:
                await subscriber.nack(message.ack_id)
            except ProviderError as error:
                raise RuntimeError(f"notification event nack failed: {error.kind.value}") from None
            except Exception:
                raise RuntimeError("notification event nack failed: unexpected") from None
    return NotificationConsumerResult(
        pulled=len(messages),
        acknowledged=acknowledged,
        failed=failed,
    )
