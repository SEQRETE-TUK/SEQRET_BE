"""Consent-backed external notification persistence and delivery tests."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.contracts.actor import ParticipantRole
from app.contracts.events import DomainEvent, DomainEventType
from app.contracts.notification import NotificationSendResult, OutboundNotification
from app.contracts.ports import ProviderError, ProviderErrorKind
from app.contracts.primitives import AggregateId, EventId, IdempotencyKey, ParticipantId
from app.modules.access.models import (
    NotificationContactChannel,
    WorkspaceAccount,
    WorkspaceContactPoint,
    WorkspaceMembership,
)
from app.modules.access.schemas import WorkspaceContactPointUpsert
from app.modules.access.workspace import delete_contact_point, upsert_contact_point
from app.modules.move_job.models import JobParticipant, MoveJob
from app.modules.notification.delivery import (
    claim_external_notifications,
    deliver_external_notifications_once,
)
from app.modules.notification.models import (
    NotificationChannel,
    NotificationDelivery,
    NotificationStatus,
)
from app.modules.notification.service import consume_notification_event, list_notifications
from app.platform.db import Base, create_session_factory

NotificationDatabase = async_sessionmaker[AsyncSession]


@pytest.fixture
async def notification_database(tmp_path: Path) -> AsyncIterator[NotificationDatabase]:
    database_path = (tmp_path / "external-notifications.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
    factory = create_session_factory(engine)
    yield factory
    await engine.dispose()


async def _seed_delivery_rows(
    factory: NotificationDatabase,
    rows: tuple[tuple[NotificationChannel, str, DomainEventType, int], ...],
    *,
    now: datetime,
) -> tuple[UUID, ...]:
    job_id = uuid4()
    participant_id = uuid4()
    notification_ids = tuple(uuid4() for _ in rows)
    async with factory.begin() as session:
        session.add(MoveJob(id=job_id, title="External notification"))
        session.add(
            JobParticipant(
                id=participant_id,
                job_id=job_id,
                role=ParticipantRole.CUSTOMER,
                display_name="Customer",
            )
        )
        session.add_all(
            NotificationDelivery(
                id=notification_id,
                event_id=uuid4(),
                event_type=event_type,
                job_id=job_id,
                recipient_participant_id=participant_id,
                channel=channel,
                destination=destination,
                status=NotificationStatus.PENDING,
                attempt_count=attempt_count,
                last_attempt_at=now if attempt_count else None,
                next_attempt_at=now,
            )
            for notification_id, (channel, destination, event_type, attempt_count) in zip(
                notification_ids,
                rows,
                strict=True,
            )
        )
    return notification_ids


class RecordingProvider:
    def __init__(self, outcomes: dict[str, Exception | str]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[OutboundNotification, str, float]] = []

    async def send(
        self,
        *,
        message: OutboundNotification,
        idempotency_key: IdempotencyKey,
        timeout_seconds: float,
    ) -> NotificationSendResult:
        self.calls.append((message, idempotency_key, timeout_seconds))
        outcome = self.outcomes[message.destination]
        if isinstance(outcome, Exception):
            raise outcome
        return NotificationSendResult(provider_message_id=outcome)


@pytest.mark.anyio
async def test_delivery_sends_all_channels_once_with_stable_links(
    notification_database: NotificationDatabase,
) -> None:
    now = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)
    rows = (
        (
            NotificationChannel.EMAIL,
            "owner@example.com",
            DomainEventType.CAPTURE_SUBMITTED_V1,
            0,
        ),
        (
            NotificationChannel.SMS,
            "+821012345678",
            DomainEventType.DISPATCH_CONFIRMED_V1,
            0,
        ),
        (
            NotificationChannel.KAKAO,
            "+821087654321",
            DomainEventType.COMPLETION_REQUESTED_V1,
            0,
        ),
    )
    notification_ids = await _seed_delivery_rows(notification_database, rows, now=now)
    provider = RecordingProvider(
        {
            "owner@example.com": "email-request",
            "+821012345678": "sms-request",
            "+821087654321": "kakao-request",
        }
    )

    result = await deliver_external_notifications_once(
        notification_database,
        provider,
        frontend_origin="https://seqret.example.com",
        batch_size=3,
        timeout_seconds=3.5,
        now=now,
    )

    assert (result.claimed, result.sent, result.retry_scheduled, result.failed) == (3, 3, 0, 0)
    assert {call[0].channel.value for call in provider.calls} == {"email", "sms", "kakao"}
    assert {call[1] for call in provider.calls} == {
        f"notification:{notification_id}" for notification_id in notification_ids
    }
    assert all(
        call[0].deep_link.startswith("https://seqret.example.com/?job=") for call in provider.calls
    )
    assert all(call[2] == 3.5 for call in provider.calls)
    async with notification_database() as session:
        persisted = (
            await session.scalars(
                select(NotificationDelivery).order_by(NotificationDelivery.destination)
            )
        ).all()
        assert all(row.status is NotificationStatus.SENT for row in persisted)
        assert all(row.attempt_count == 1 for row in persisted)
        assert {row.provider_message_id for row in persisted} == {
            "email-request",
            "sms-request",
            "kakao-request",
        }
    repeated = await deliver_external_notifications_once(
        notification_database,
        provider,
        frontend_origin="https://seqret.example.com",
        now=now + timedelta(days=1),
    )
    assert repeated.claimed == repeated.sent == 0


@pytest.mark.anyio
async def test_delivery_retries_transient_errors_and_stops_terminal_errors(
    notification_database: NotificationDatabase,
) -> None:
    now = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)
    destinations = (
        "retry@example.com",
        "+821011111111",
        "+821022222222",
        "+821033333333",
    )
    await _seed_delivery_rows(
        notification_database,
        (
            (NotificationChannel.EMAIL, destinations[0], DomainEventType.ANALYSIS_FAILED_V1, 0),
            (NotificationChannel.SMS, destinations[1], DomainEventType.SCOPE_LOCKED_V1, 0),
            (NotificationChannel.KAKAO, destinations[2], DomainEventType.CHANGE_REQUESTED_V1, 0),
            (NotificationChannel.SMS, destinations[3], DomainEventType.MEDIA_DELETED_V1, 4),
        ),
        now=now,
    )
    provider = RecordingProvider(
        {
            destinations[0]: ProviderError(
                ProviderErrorKind.UNAVAILABLE,
                "temporary",
                retryable=True,
            ),
            destinations[1]: ProviderError(
                ProviderErrorKind.INVALID_INPUT,
                "terminal",
                retryable=False,
            ),
            destinations[2]: RuntimeError("unexpected provider failure"),
            destinations[3]: ProviderError(
                ProviderErrorKind.UNAVAILABLE,
                "retry budget exhausted",
                retryable=True,
            ),
        }
    )

    result = await deliver_external_notifications_once(
        notification_database,
        provider,
        frontend_origin="https://seqret.example.com",
        now=now,
    )

    assert (result.claimed, result.sent, result.retry_scheduled, result.failed) == (4, 0, 2, 2)
    async with notification_database() as session:
        persisted = {
            row.destination: row
            for row in (await session.scalars(select(NotificationDelivery))).all()
        }
        for destination in (destinations[0], destinations[2]):
            row = persisted[destination]
            assert row.status is NotificationStatus.PENDING
            assert row.last_error_code is None
            assert row.next_attempt_at is not None
            assert row.next_attempt_at.replace(tzinfo=UTC) == now + timedelta(seconds=1)
        assert persisted[destinations[1]].status is NotificationStatus.FAILED
        assert persisted[destinations[1]].last_error_code == "invalid_input"
        assert persisted[destinations[3]].status is NotificationStatus.FAILED
        assert persisted[destinations[3]].last_error_code == "unavailable"
        assert persisted[destinations[3]].attempt_count == 5


@pytest.mark.anyio
async def test_delivery_ignores_claims_changed_while_provider_is_running(
    notification_database: NotificationDatabase,
) -> None:
    now = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)
    notification_ids = await _seed_delivery_rows(
        notification_database,
        (
            (
                NotificationChannel.EMAIL,
                "stale-success@example.com",
                DomainEventType.ANALYSIS_COMPLETED_V1,
                0,
            ),
            (
                NotificationChannel.EMAIL,
                "stale-failure@example.com",
                DomainEventType.COMPLETION_DECIDED_V1,
                0,
            ),
        ),
        now=now,
    )

    class DeletingProvider:
        async def send(
            self,
            *,
            message: OutboundNotification,
            idempotency_key: IdempotencyKey,
            timeout_seconds: float,
        ) -> NotificationSendResult:
            del idempotency_key, timeout_seconds
            async with notification_database.begin() as session:
                row = await session.get(NotificationDelivery, message.notification_id)
                assert row is not None
                await session.delete(row)
            if "failure" in message.destination:
                raise ProviderError(
                    ProviderErrorKind.UNAVAILABLE,
                    "stale",
                    retryable=True,
                )
            return NotificationSendResult(provider_message_id="already-removed")

    result = await deliver_external_notifications_once(
        notification_database,
        DeletingProvider(),
        frontend_origin="https://seqret.example.com",
        now=now,
    )
    assert result.claimed == 2
    assert result.sent == result.retry_scheduled == result.failed == 0
    async with notification_database() as session:
        for notification_id in notification_ids:
            assert await session.get(NotificationDelivery, notification_id) is None


@pytest.mark.anyio
async def test_claim_validates_bounds(notification_database: NotificationDatabase) -> None:
    async with notification_database.begin() as session:
        with pytest.raises(ValueError, match="limit"):
            await claim_external_notifications(
                session,
                frontend_origin="https://seqret.example.com",
                limit=0,
            )
        with pytest.raises(ValueError, match="lease_seconds"):
            await claim_external_notifications(
                session,
                frontend_origin="https://seqret.example.com",
                lease_seconds=0,
            )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("batch_size", "timeout_seconds", "lease_seconds"),
    [
        (0, 10.0, 60),
        (101, 10.0, 60),
        (100, 0.0, 60),
        (100, 10.0, 11),
    ],
)
async def test_delivery_validates_bounds(
    notification_database: NotificationDatabase,
    batch_size: int,
    timeout_seconds: float,
    lease_seconds: int,
) -> None:
    with pytest.raises(ValueError):
        await deliver_external_notifications_once(
            notification_database,
            RecordingProvider({}),
            frontend_origin="https://seqret.example.com",
            batch_size=batch_size,
            timeout_seconds=timeout_seconds,
            lease_seconds=lease_seconds,
        )


@pytest.mark.anyio
async def test_event_consumption_snapshots_contacts_and_revocation_cancels_pending(
    notification_database: NotificationDatabase,
) -> None:
    now = datetime(2026, 8, 19, 11, 0, tzinfo=UTC)
    job_id = uuid4()
    customer_id = uuid4()
    worker_id = uuid4()
    account_id = uuid4()
    async with notification_database.begin() as session:
        session.add(MoveJob(id=job_id, title="Contact snapshot"))
        session.add_all(
            (
                JobParticipant(
                    id=customer_id,
                    job_id=job_id,
                    role=ParticipantRole.CUSTOMER,
                    display_name="Customer",
                ),
                JobParticipant(
                    id=worker_id,
                    job_id=job_id,
                    role=ParticipantRole.FIELD_WORKER,
                    display_name="Worker",
                ),
                WorkspaceAccount(
                    id=account_id,
                    role=ParticipantRole.FIELD_WORKER,
                    display_name="Worker account",
                    created_at=now,
                ),
                WorkspaceMembership(
                    account_id=account_id,
                    participant_id=worker_id,
                    joined_at=now,
                ),
                WorkspaceContactPoint(
                    account_id=account_id,
                    channel=NotificationContactChannel.EMAIL,
                    destination="worker@example.com",
                    enabled=True,
                    consented_at=now,
                    updated_at=now,
                ),
            )
        )
    event = DomainEvent(
        event_id=EventId(uuid4()),
        event_type=DomainEventType.SCOPE_LOCKED_V1,
        aggregate_id=AggregateId(job_id),
        actor_id=ParticipantId(customer_id),
        trace_id="0123456789abcdef0123456789abcdef",
        payload={"scope_version_id": str(uuid4()), "content_hash": "a" * 64},
    )
    async with notification_database.begin() as session:
        created = await consume_notification_event(session, event)
        assert {item.channel for item in created} == {
            NotificationChannel.IN_APP,
            NotificationChannel.EMAIL,
        }
    async with notification_database.begin() as session:
        assert await consume_notification_event(session, event) == ()
        visible = await list_notifications(session, job_id, worker_id)
        assert len(visible) == 1
        assert visible[0].channel is NotificationChannel.IN_APP
        unchanged = await upsert_contact_point(
            session,
            account_id,
            NotificationContactChannel.EMAIL,
            WorkspaceContactPointUpsert(
                destination="worker@example.com",
                delivery_consent=True,
            ),
        )
        assert unchanged.masked_destination == "wo****@example.com"
        changed = await upsert_contact_point(
            session,
            account_id,
            NotificationContactChannel.EMAIL,
            WorkspaceContactPointUpsert(
                destination="new-worker@example.com",
                delivery_consent=True,
            ),
        )
        assert changed.masked_destination.endswith("@example.com")
    async with notification_database() as session:
        external = await session.scalar(
            select(NotificationDelivery).where(
                NotificationDelivery.channel == NotificationChannel.EMAIL
            )
        )
        assert external is not None
        assert external.status is NotificationStatus.FAILED
        assert external.last_error_code == "consent_revoked"
        assert external.destination == "revoked:email"

    second_event = event.model_copy(update={"event_id": EventId(uuid4())})
    async with notification_database.begin() as session:
        created_again = await consume_notification_event(session, second_event)
        assert len(created_again) == 2
        await delete_contact_point(session, account_id, NotificationContactChannel.EMAIL)
    async with notification_database() as session:
        latest_external = await session.scalar(
            select(NotificationDelivery)
            .where(NotificationDelivery.channel == NotificationChannel.EMAIL)
            .order_by(NotificationDelivery.created_at.desc(), NotificationDelivery.id.desc())
            .limit(1)
        )
        assert latest_external is not None
        assert latest_external.status is NotificationStatus.FAILED
        assert latest_external.last_error_code == "consent_revoked"


@pytest.mark.anyio
async def test_event_consumption_records_receipt_when_every_recipient_is_excluded(
    notification_database: NotificationDatabase,
) -> None:
    job_id = uuid4()
    worker_id = uuid4()
    async with notification_database.begin() as session:
        session.add(MoveJob(id=job_id, title="No recipient"))
        session.add(
            JobParticipant(
                id=worker_id,
                job_id=job_id,
                role=ParticipantRole.FIELD_WORKER,
                display_name="Only worker",
            )
        )
    event = DomainEvent(
        event_id=EventId(uuid4()),
        event_type=DomainEventType.SCOPE_LOCKED_V1,
        aggregate_id=AggregateId(job_id),
        actor_id=ParticipantId(worker_id),
        trace_id="0123456789abcdef0123456789abcdef",
        payload={"scope_version_id": str(uuid4()), "content_hash": "b" * 64},
    )
    async with notification_database.begin() as session:
        assert await consume_notification_event(session, event) == ()
