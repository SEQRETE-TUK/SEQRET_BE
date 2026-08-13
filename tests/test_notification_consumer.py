"""Notification consumer transaction and acknowledgement tests."""

from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from traceback import format_exception
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.contracts.actor import ParticipantRole
from app.contracts.events import DomainEvent, DomainEventType
from app.contracts.ports import ProviderError, ProviderErrorKind
from app.contracts.primitives import AggregateId, EventId
from app.modules.move_job.models import JobParticipant, MoveJob
from app.modules.notification.consumer import (
    NotificationConsumerResult,
    PulledEventMessage,
    consume_notification_events_once,
    parse_domain_event,
)
from app.modules.notification.models import EventConsumption, NotificationDelivery
from app.platform.db import Base, create_session_factory

ConsumerDatabase = async_sessionmaker[AsyncSession]


@pytest.fixture
async def consumer_database(tmp_path: Path) -> AsyncIterator[ConsumerDatabase]:
    database_path = (tmp_path / "consumer.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
    factory = create_session_factory(engine)
    yield factory
    await engine.dispose()


def _event(job_id: UUID, *, event_id: UUID | None = None) -> DomainEvent:
    return DomainEvent(
        event_id=EventId(event_id or uuid4()),
        event_type=DomainEventType.CHANGE_REQUESTED_V1,
        aggregate_id=AggregateId(job_id),
        trace_id="0123456789abcdef0123456789abcdef",
        payload={
            "change_request_id": str(uuid4()),
            "base_scope_version_id": str(uuid4()),
            "evidence_media_asset_ids": [str(uuid4())],
        },
    )


def _message(
    event: DomainEvent, *, attributes: Mapping[str, str] | None = None
) -> PulledEventMessage:
    return PulledEventMessage(
        ack_id=f"ack-{event.event_id}",
        data=event.model_dump_json().encode(),
        attributes=attributes
        or {
            "event_id": str(event.event_id),
            "event_type": event.event_type.value,
            "schema_version": str(event.schema_version),
            "idempotency_key": f"event:{event.event_id}",
        },
    )


class FakeSubscriber:
    def __init__(
        self,
        messages: tuple[PulledEventMessage, ...],
        *,
        database: ConsumerDatabase | None = None,
        fail_ack: bool = False,
        nack_error: Exception | None = None,
    ) -> None:
        self.messages = messages
        self.database = database
        self.fail_ack = fail_ack
        self.nack_error = nack_error
        self.pull_args: tuple[int, float] | None = None
        self.acked: list[str] = []
        self.nacked: list[str] = []

    async def pull(
        self,
        *,
        max_messages: int,
        timeout_seconds: float,
    ) -> tuple[PulledEventMessage, ...]:
        self.pull_args = (max_messages, timeout_seconds)
        return self.messages

    async def acknowledge(self, ack_id: str) -> None:
        if self.database is not None:
            async with self.database() as session:
                assert await session.scalar(select(func.count()).select_from(EventConsumption)) == 1
                assert (
                    await session.scalar(select(func.count()).select_from(NotificationDelivery))
                    == 1
                )
        if self.fail_ack:
            raise RuntimeError("ack unavailable")
        self.acked.append(ack_id)

    async def nack(self, ack_id: str) -> None:
        self.nacked.append(ack_id)
        if self.nack_error is not None:
            raise self.nack_error


async def _seed_job(database: ConsumerDatabase, job_id: UUID) -> None:
    async with database.begin() as session:
        session.add(MoveJob(id=job_id, title="Consumer test"))
        session.add(
            JobParticipant(
                id=uuid4(),
                job_id=job_id,
                role=ParticipantRole.CUSTOMER,
                display_name="Customer",
            )
        )


@pytest.mark.anyio
async def test_consumer_commits_before_ack_and_deduplicates_redelivery(
    consumer_database: ConsumerDatabase,
) -> None:
    job_id = uuid4()
    await _seed_job(consumer_database, job_id)
    message = _message(_event(job_id))
    subscriber = FakeSubscriber((message, message), database=consumer_database)

    result = await consume_notification_events_once(
        consumer_database,
        subscriber,
        batch_size=20,
        pull_timeout_seconds=4,
    )

    assert result == NotificationConsumerResult(pulled=2, acknowledged=2, failed=0)
    assert subscriber.pull_args == (20, 4)
    assert subscriber.acked == [message.ack_id, message.ack_id]
    assert subscriber.nacked == []
    async with consumer_database() as session:
        assert await session.scalar(select(func.count()).select_from(EventConsumption)) == 1
        assert await session.scalar(select(func.count()).select_from(NotificationDelivery)) == 1


@pytest.mark.anyio
async def test_consumer_nacks_invalid_event_without_persisting(
    consumer_database: ConsumerDatabase,
) -> None:
    message = PulledEventMessage(ack_id="poison", data=b"{}", attributes={})
    subscriber = FakeSubscriber((message,))

    result = await consume_notification_events_once(
        consumer_database,
        subscriber,
        batch_size=1,
        pull_timeout_seconds=1,
    )

    assert result == NotificationConsumerResult(pulled=1, acknowledged=0, failed=1)
    assert subscriber.nacked == ["poison"]
    async with consumer_database() as session:
        assert await session.scalar(select(func.count()).select_from(EventConsumption)) == 0


@pytest.mark.anyio
async def test_consumer_nacks_after_ack_failure_and_keeps_committed_receipt(
    consumer_database: ConsumerDatabase,
) -> None:
    job_id = uuid4()
    await _seed_job(consumer_database, job_id)
    message = _message(_event(job_id))
    subscriber = FakeSubscriber((message,), fail_ack=True)

    result = await consume_notification_events_once(
        consumer_database,
        subscriber,
        batch_size=1,
        pull_timeout_seconds=1,
    )

    assert result.failed == 1
    assert subscriber.nacked == [message.ack_id]
    async with consumer_database() as session:
        assert await session.scalar(select(func.count()).select_from(EventConsumption)) == 1


@pytest.mark.parametrize(
    ("nack_error", "safe_code"),
    [
        (RuntimeError("nack credential"), "unexpected"),
        (
            ProviderError(
                ProviderErrorKind.UNAVAILABLE,
                "nack credential",
                retryable=True,
            ),
            "unavailable",
        ),
    ],
)
@pytest.mark.anyio
async def test_consumer_sanitizes_nack_failure(
    consumer_database: ConsumerDatabase,
    nack_error: Exception,
    safe_code: str,
) -> None:
    subscriber = FakeSubscriber(
        (
            PulledEventMessage(
                ack_id="poison",
                data=b'{"credential":"raw-secret"}',
                attributes={},
            ),
        ),
        nack_error=nack_error,
    )

    with pytest.raises(
        RuntimeError,
        match=f"notification event nack failed: {safe_code}",
    ) as error_info:
        await consume_notification_events_once(
            consumer_database,
            subscriber,
            batch_size=1,
            pull_timeout_seconds=1,
        )

    formatted = "".join(format_exception(error_info.value))
    assert error_info.value.__suppress_context__
    assert "raw-secret" not in formatted
    assert "nack credential" not in formatted


def test_domain_event_parser_rejects_attribute_drift() -> None:
    event = _event(uuid4())
    message = _message(event, attributes={"event_id": str(event.event_id)})

    with pytest.raises(ValueError, match="attributes do not match"):
        parse_domain_event(message)

    assert parse_domain_event(_message(event)) == event
