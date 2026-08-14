"""Transactional Outbox relay and notification consumer tests."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import AppEnvironment, Settings
from app.contracts.actor import ParticipantRole
from app.contracts.events import DomainEvent, DomainEventType
from app.contracts.fakes import FakeEventBus
from app.contracts.ports import ProviderError, ProviderErrorKind
from app.contracts.primitives import AggregateId, EventId, ParticipantId
from app.main import create_app
from app.modules.move_job.models import JobParticipant, MoveJob
from app.modules.notification.models import (
    EventConsumption,
    NotificationDelivery,
    NotificationStatus,
)
from app.modules.notification.service import (
    NotificationConflictError,
    NotificationResourceNotFoundError,
    consume_notification_event,
    list_notifications,
    mark_notification_failed,
    mark_notification_sent,
    retry_notification,
)
from app.platform.db import Base, create_session_factory
from app.platform.event_bus.models import OutboxEvent
from app.platform.event_bus.service import (
    _delivery_state,
    _require_delivery_transition,
    claim_outbox_events,
    enqueue_domain_event,
    mark_outbox_failed,
    mark_outbox_published,
    relay_outbox_once,
)

OutboxDatabase = async_sessionmaker[AsyncSession]


@pytest.fixture
async def outbox_database(tmp_path: Path) -> AsyncIterator[OutboxDatabase]:
    database_path = (tmp_path / "outbox.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
    factory = create_session_factory(engine)
    yield factory
    await engine.dispose()


async def _job(factory: OutboxDatabase) -> tuple[UUID, dict[ParticipantRole, UUID]]:
    job_id = uuid4()
    participant_ids = {role: uuid4() for role in ParticipantRole}
    async with factory.begin() as session:
        session.add(MoveJob(id=job_id, title="Outbox test"))
        session.add_all(
            JobParticipant(
                id=participant_id,
                job_id=job_id,
                role=role,
                display_name=role.value,
            )
            for role, participant_id in participant_ids.items()
        )
    return job_id, participant_ids


def _event_payload(event_type: DomainEventType) -> dict[str, Any]:
    resource_id = str(uuid4())
    if event_type is DomainEventType.CAPTURE_SUBMITTED_V1:
        return {
            "capture_session_id": resource_id,
            "analysis_run_id": str(uuid4()),
            "inventory_media_asset_ids": [str(uuid4())],
        }
    if event_type is DomainEventType.ANALYSIS_COMPLETED_V1:
        return {
            "capture_session_id": resource_id,
            "analysis_run_id": str(uuid4()),
            "scope_version_id": str(uuid4()),
        }
    if event_type is DomainEventType.ANALYSIS_FAILED_V1:
        return {
            "capture_session_id": resource_id,
            "analysis_run_id": str(uuid4()),
            "error_kind": "unavailable",
            "retryable": True,
        }
    if event_type is DomainEventType.SCOPE_LOCKED_V1:
        return {"scope_version_id": resource_id, "content_hash": "a" * 64}
    if event_type is DomainEventType.CHANGE_REQUESTED_V1:
        return {
            "change_request_id": resource_id,
            "base_scope_version_id": str(uuid4()),
            "evidence_media_asset_ids": [str(uuid4())],
        }
    if event_type is DomainEventType.COMPLETION_MEDIA_SUBMITTED_V1:
        return {
            "capture_session_id": resource_id,
            "media_asset_id": str(uuid4()),
            "room_zone_id": str(uuid4()),
        }
    if event_type is DomainEventType.MEDIA_DELETED_V1:
        return {"background_job_id": resource_id, "media_asset_id": str(uuid4())}
    return {"resource_id": resource_id}


def _event(
    job_id: UUID,
    event_type: DomainEventType,
    *,
    actor_id: UUID | None = None,
    event_id: UUID | None = None,
) -> DomainEvent:
    return DomainEvent(
        event_id=EventId(event_id or uuid4()),
        event_type=event_type,
        aggregate_id=AggregateId(job_id),
        actor_id=ParticipantId(actor_id) if actor_id else None,
        trace_id="0123456789abcdef0123456789abcdef",
        payload=_event_payload(event_type),
    )


@pytest.mark.anyio
async def test_business_rollback_removes_outbox_event(outbox_database: OutboxDatabase) -> None:
    job_id, participants = await _job(outbox_database)
    event_id: UUID
    with pytest.raises(RuntimeError, match="rollback"):
        async with outbox_database.begin() as session:
            event = enqueue_domain_event(
                session,
                DomainEventType.SCOPE_LOCKED_V1,
                job_id,
                actor_id=participants[ParticipantRole.CUSTOMER],
                trace_id="0123456789abcdef0123456789abcdef",
                payload={"scope_version_id": str(uuid4()), "content_hash": "a" * 64},
            )
            event_id = event.event_id
            await session.flush()
            raise RuntimeError("rollback")

    async with outbox_database() as session:
        assert await session.get(OutboxEvent, event_id) is None


@pytest.mark.anyio
async def test_outbox_relay_retries_failure_and_publishes_once(
    outbox_database: OutboxDatabase,
) -> None:
    job_id, participants = await _job(outbox_database)
    start = datetime(2026, 8, 12, 6, 0, tzinfo=UTC)
    async with outbox_database.begin() as session:
        event = enqueue_domain_event(
            session,
            DomainEventType.CHANGE_REQUESTED_V1,
            job_id,
            actor_id=participants[ParticipantRole.FIELD_WORKER],
            trace_id="0123456789abcdef0123456789abcdef",
            payload={
                "change_request_id": str(uuid4()),
                "base_scope_version_id": str(uuid4()),
                "evidence_media_asset_ids": [str(uuid4())],
            },
            occurred_at=start,
        )

    class FailingBus:
        async def publish(self, **_: object) -> None:
            raise ProviderError(
                ProviderErrorKind.UNAVAILABLE,
                "temporary failure",
                retryable=True,
            )

    first = await relay_outbox_once(outbox_database, FailingBus(), now=start)
    assert first.claimed == first.failed == 1
    assert first.published == 0
    async with outbox_database() as session:
        row = await session.get(OutboxEvent, event.event_id)
        assert row is not None
        assert row.attempt_count == 1
        assert row.next_attempt_at.replace(tzinfo=UTC) == start + timedelta(seconds=1)
        assert row.last_error_code == "unavailable"
        assert row.lock_token is None

    event_bus = FakeEventBus()
    assert (await relay_outbox_once(outbox_database, event_bus, now=start)).claimed == 0
    second = await relay_outbox_once(
        outbox_database,
        event_bus,
        now=start + timedelta(seconds=1),
    )
    assert second.claimed == second.published == 1
    assert second.failed == 0
    assert list(event_bus.published.values()) == [event]
    assert (
        await relay_outbox_once(outbox_database, event_bus, now=start + timedelta(days=1))
    ).claimed == 0


@pytest.mark.anyio
async def test_outbox_relay_starts_all_leased_publications_without_queueing(
    outbox_database: OutboxDatabase,
) -> None:
    job_id, _ = await _job(outbox_database)
    start = datetime(2026, 8, 12, 6, 30, tzinfo=UTC)
    async with outbox_database.begin() as session:
        for event_type in (
            DomainEventType.SCOPE_LOCKED_V1,
            DomainEventType.CHANGE_REQUESTED_V1,
        ):
            enqueue_domain_event(
                session,
                event_type,
                job_id,
                occurred_at=start,
                payload=_event_payload(event_type),
            )

    class BarrierBus:
        def __init__(self) -> None:
            self.active = 0
            self.all_started = asyncio.Event()
            self.release = asyncio.Event()

        async def publish(self, **_: object) -> None:
            self.active += 1
            if self.active == 2:
                self.all_started.set()
            try:
                await self.release.wait()
            finally:
                self.active -= 1

    event_bus = BarrierBus()
    relay = asyncio.create_task(
        relay_outbox_once(outbox_database, event_bus, now=start, batch_size=2)
    )
    await asyncio.wait_for(event_bus.all_started.wait(), timeout=1)
    event_bus.release.set()

    result = await relay
    assert (result.claimed, result.published, result.failed) == (2, 2, 0)


@pytest.mark.anyio
async def test_claim_recovers_expired_lease_and_rejects_stale_owner(
    outbox_database: OutboxDatabase,
) -> None:
    job_id, _ = await _job(outbox_database)
    start = datetime(2026, 8, 12, 7, 0, tzinfo=UTC)
    async with outbox_database.begin() as session:
        enqueue_domain_event(
            session,
            DomainEventType.SCOPE_LOCKED_V1,
            job_id,
            occurred_at=start,
            payload={"scope_version_id": str(uuid4()), "content_hash": "a" * 64},
        )
    async with outbox_database.begin() as session:
        first = (await claim_outbox_events(session, now=start, lease_seconds=10))[0]
    async with outbox_database.begin() as session:
        assert await claim_outbox_events(session, now=start + timedelta(seconds=9)) == ()
    async with outbox_database.begin() as session:
        second = (await claim_outbox_events(session, now=start + timedelta(seconds=10)))[0]

    assert second.event == first.event
    assert second.lock_token != first.lock_token
    async with outbox_database.begin() as session:
        assert not await mark_outbox_published(session, first.event.event_id, first.lock_token)
        assert not await mark_outbox_failed(
            session,
            first.event.event_id,
            first.lock_token,
            "stale",
        )
        assert await mark_outbox_published(session, second.event.event_id, second.lock_token)


@pytest.mark.anyio
async def test_notification_consumer_deduplicates_and_tracks_delivery(
    outbox_database: OutboxDatabase,
) -> None:
    job_id, participants = await _job(outbox_database)
    event = _event(
        job_id,
        DomainEventType.CHANGE_REQUESTED_V1,
        actor_id=participants[ParticipantRole.FIELD_WORKER],
    )
    async with outbox_database.begin() as session:
        created = await consume_notification_event(session, event)
    assert {item.recipient_participant_id for item in created} == {
        participants[ParticipantRole.CUSTOMER],
        participants[ParticipantRole.COMPANY_MANAGER],
    }
    async with outbox_database.begin() as session:
        assert await consume_notification_event(session, event) == ()
        failed = await mark_notification_failed(session, created[0].id, "provider_unavailable")
        assert failed.status is NotificationStatus.FAILED
        assert failed.attempt_count == 1
        retried = await retry_notification(session, created[0].id)
        assert retried.status is NotificationStatus.PENDING
        sent = await mark_notification_sent(session, created[0].id)
        assert sent.status is NotificationStatus.SENT
        assert sent.attempt_count == 2
        assert await mark_notification_sent(session, created[0].id) == sent
        with pytest.raises(NotificationConflictError):
            await mark_notification_failed(session, created[0].id, "provider_unavailable")
        with pytest.raises(NotificationConflictError):
            await retry_notification(session, created[1].id)

    async with outbox_database() as session:
        customer = await list_notifications(
            session,
            job_id,
            participants[ParticipantRole.CUSTOMER],
        )
        manager = await list_notifications(
            session,
            job_id,
            participants[ParticipantRole.COMPANY_MANAGER],
        )
        assert len(customer) == len(manager) == 1
        assert len((await session.scalars(select(EventConsumption))).all()) == 1
        assert len((await session.scalars(select(NotificationDelivery))).all()) == 2


@pytest.mark.anyio
async def test_notification_consumer_handles_ignored_event_and_missing_job(
    outbox_database: OutboxDatabase,
) -> None:
    job_id, _ = await _job(outbox_database)
    async with outbox_database.begin() as session:
        assert (
            await consume_notification_event(
                session,
                _event(job_id, DomainEventType.MEDIA_DELETED_V1),
            )
            == ()
        )
    async with outbox_database.begin() as session:
        with pytest.raises(NotificationResourceNotFoundError):
            await consume_notification_event(
                session,
                _event(uuid4(), DomainEventType.SCOPE_LOCKED_V1),
            )


@pytest.mark.anyio
async def test_outbox_and_notification_input_boundaries(
    outbox_database: OutboxDatabase,
) -> None:
    job_id, _ = await _job(outbox_database)
    async with outbox_database.begin() as session:
        with pytest.raises(ValueError, match="between"):
            await claim_outbox_events(session, limit=0)
        with pytest.raises(ValueError, match="between"):
            await claim_outbox_events(session, limit=101)
        with pytest.raises(ValueError, match="positive"):
            await claim_outbox_events(session, lease_seconds=0)
        with pytest.raises(ValueError, match="error_code"):
            await mark_outbox_failed(session, uuid4(), uuid4(), "")
        with pytest.raises(ValueError, match="lowercase"):
            await mark_notification_failed(session, uuid4(), "INVALID CODE")
        with pytest.raises(NotificationResourceNotFoundError):
            await mark_notification_sent(session, uuid4())
    with pytest.raises(ValueError, match="positive"):
        await relay_outbox_once(outbox_database, FakeEventBus(), publish_timeout_seconds=0)
    with pytest.raises(ValueError, match="exceed"):
        await relay_outbox_once(
            outbox_database,
            FakeEventBus(),
            lease_seconds=10,
            publish_timeout_seconds=10,
        )
    assert job_id


def test_outbox_transition_guard_rejects_published_reentry() -> None:
    row = OutboxEvent(
        event_id=uuid4(),
        event_type=DomainEventType.SCOPE_LOCKED_V1,
        schema_version=1,
        aggregate_id=uuid4(),
        trace_id="0123456789abcdef0123456789abcdef",
        payload={},
        occurred_at=datetime.now(UTC),
        next_attempt_at=datetime.now(UTC),
        published_at=datetime.now(UTC),
    )

    assert _delivery_state(row) == "published"
    with pytest.raises(ValueError, match="published->leased"):
        _require_delivery_transition(row, "leased")


@pytest.mark.anyio
async def test_notification_api_returns_only_current_participant_intents(
    outbox_database: OutboxDatabase,
) -> None:
    application = create_app(Settings(environment=AppEnvironment.TEST))
    application.state.database_session_factory = outbox_database
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        created_response = await client.post(
            "/api/v1/move-jobs",
            json={
                "title": "Notification API",
                "participants": [
                    {"role": "customer", "display_name": "Customer"},
                    {"role": "company_manager", "display_name": "Manager"},
                    {"role": "field_worker", "display_name": "Field worker"},
                ],
                "locations": [
                    {
                        "kind": "origin",
                        "label": "Origin",
                        "room_zones": [{"name": "Living room", "sort_order": 0}],
                    }
                ],
            },
        )
        assert created_response.status_code == 201
        created = cast(dict[str, Any], created_response.json())
        customer_link = next(link for link in created["access_links"] if link["role"] == "customer")
        manager_id = UUID(
            next(
                participant["id"]
                for participant in created["job"]["participants"]
                if participant["role"] == "company_manager"
            )
        )
        event = _event(
            UUID(created["job"]["id"]),
            DomainEventType.CHANGE_REQUESTED_V1,
            actor_id=uuid4(),
        )
        async with outbox_database.begin() as session:
            await consume_notification_event(session, event)

        response = await client.get(
            f"/api/v1/move-jobs/{created['job']['id']}/notifications",
            headers={"Authorization": f"Bearer {customer_link['secret']}"},
        )
        assert response.status_code == 200
        assert len(response.json()) == 1
        assert response.json()[0]["recipient_participant_id"] != str(manager_id)
        cross_job = await client.get(
            f"/api/v1/move-jobs/{uuid4()}/notifications",
            headers={"Authorization": f"Bearer {customer_link['secret']}"},
        )
        assert cross_job.status_code == 404
