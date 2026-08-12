"""Transactional Outbox enqueue and lease-based relay operations."""

import asyncio
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.events import DomainEvent, DomainEventType
from app.contracts.ports import EventBusPort, ProviderError
from app.contracts.primitives import (
    AggregateId,
    EventId,
    IdempotencyKey,
    ParticipantId,
    utc_now,
)
from app.platform.event_bus.models import OutboxEvent

DEFAULT_BATCH_SIZE = 100
MAX_BATCH_SIZE = 100
DEFAULT_LEASE_SECONDS = 60
DEFAULT_PUBLISH_TIMEOUT_SECONDS = 10.0
MAX_RETRY_DELAY_SECONDS = 300
ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
OUTBOX_DELIVERY_TRANSITIONS = {
    "ready": frozenset({"leased"}),
    "leased": frozenset({"ready", "leased", "published"}),
    "published": frozenset(),
}


@dataclass(frozen=True, slots=True)
class ClaimedOutboxEvent:
    """An event and its lease token carried outside the claim transaction."""

    event: DomainEvent
    lock_token: UUID


@dataclass(frozen=True, slots=True)
class RelayResult:
    """Counts from one bounded relay pass."""

    claimed: int
    published: int
    failed: int


def _delivery_state(row: OutboxEvent) -> str:
    if row.published_at is not None:
        return "published"
    if row.lock_token is not None:
        return "leased"
    return "ready"


def _require_delivery_transition(row: OutboxEvent, target: str) -> None:
    current = _delivery_state(row)
    if target not in OUTBOX_DELIVERY_TRANSITIONS[current]:
        raise ValueError(f"invalid outbox transition: {current}->{target}")


def enqueue_domain_event(
    session: AsyncSession,
    event_type: DomainEventType,
    aggregate_id: UUID,
    *,
    actor_id: UUID | None = None,
    trace_id: str | None = None,
    payload: Mapping[str, JsonValue] | None = None,
    occurred_at: datetime | None = None,
) -> DomainEvent:
    """Persist a versioned event inside the caller's business transaction."""

    event = DomainEvent(
        event_id=EventId(uuid4()),
        event_type=event_type,
        aggregate_id=AggregateId(aggregate_id),
        occurred_at=occurred_at or utc_now(),
        actor_id=ParticipantId(actor_id) if actor_id is not None else None,
        trace_id=trace_id or secrets.token_hex(16),
        payload=dict(payload or {}),
    )
    session.add(
        OutboxEvent(
            event_id=event.event_id,
            event_type=event.event_type,
            schema_version=event.schema_version,
            aggregate_id=event.aggregate_id,
            actor_id=event.actor_id,
            trace_id=event.trace_id,
            payload=event.payload,
            occurred_at=event.occurred_at,
            next_attempt_at=event.occurred_at,
        )
    )
    return event


def _to_domain_event(row: OutboxEvent) -> DomainEvent:
    occurred_at = (
        row.occurred_at.replace(tzinfo=UTC) if row.occurred_at.tzinfo is None else row.occurred_at
    )
    return DomainEvent(
        event_id=EventId(row.event_id),
        event_type=row.event_type,
        schema_version=row.schema_version,
        aggregate_id=AggregateId(row.aggregate_id),
        occurred_at=occurred_at,
        actor_id=ParticipantId(row.actor_id) if row.actor_id is not None else None,
        trace_id=row.trace_id,
        payload=row.payload,
    )


async def claim_outbox_events(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = DEFAULT_BATCH_SIZE,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> tuple[ClaimedOutboxEvent, ...]:
    """Lease due unpublished events without blocking another relay worker."""

    if not 1 <= limit <= MAX_BATCH_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_BATCH_SIZE}")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    claimed_at = now or utc_now()
    rows = (
        await session.scalars(
            select(OutboxEvent)
            .where(
                OutboxEvent.published_at.is_(None),
                OutboxEvent.next_attempt_at <= claimed_at,
                or_(
                    OutboxEvent.locked_until.is_(None),
                    OutboxEvent.locked_until <= claimed_at,
                ),
            )
            .order_by(OutboxEvent.occurred_at, OutboxEvent.event_id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    claims: list[ClaimedOutboxEvent] = []
    for row in rows:
        _require_delivery_transition(row, "leased")
        lock_token = uuid4()
        row.lock_token = lock_token
        row.locked_until = claimed_at + timedelta(seconds=lease_seconds)
        row.attempt_count += 1
        claims.append(ClaimedOutboxEvent(event=_to_domain_event(row), lock_token=lock_token))
    await session.flush()
    return tuple(claims)


async def mark_outbox_published(
    session: AsyncSession,
    event_id: UUID,
    lock_token: UUID,
    *,
    published_at: datetime | None = None,
) -> bool:
    """Finalize a publish only when the caller still owns the event lease."""

    row = await session.scalar(
        select(OutboxEvent)
        .where(
            OutboxEvent.event_id == event_id,
            OutboxEvent.lock_token == lock_token,
            OutboxEvent.published_at.is_(None),
        )
        .with_for_update()
    )
    if row is None:
        return False
    _require_delivery_transition(row, "published")
    row.published_at = published_at or utc_now()
    row.lock_token = None
    row.locked_until = None
    row.last_error_code = None
    await session.flush()
    return True


async def mark_outbox_failed(
    session: AsyncSession,
    event_id: UUID,
    lock_token: UUID,
    error_code: str,
    *,
    failed_at: datetime | None = None,
) -> bool:
    """Release a failed event with bounded exponential retry delay."""

    if ERROR_CODE_PATTERN.fullmatch(error_code) is None:
        raise ValueError("error_code must be a lowercase identifier")
    row = await session.scalar(
        select(OutboxEvent)
        .where(
            OutboxEvent.event_id == event_id,
            OutboxEvent.lock_token == lock_token,
            OutboxEvent.published_at.is_(None),
        )
        .with_for_update()
    )
    if row is None:
        return False
    _require_delivery_transition(row, "ready")
    recorded_at = failed_at or utc_now()
    retry_delay = min(2 ** min(max(row.attempt_count - 1, 0), 8), MAX_RETRY_DELAY_SECONDS)
    row.next_attempt_at = recorded_at + timedelta(seconds=retry_delay)
    row.lock_token = None
    row.locked_until = None
    row.last_error_code = error_code
    await session.flush()
    return True


async def relay_outbox_once(
    factory: async_sessionmaker[AsyncSession],
    event_bus: EventBusPort,
    *,
    now: datetime | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    publish_timeout_seconds: float = DEFAULT_PUBLISH_TIMEOUT_SECONDS,
) -> RelayResult:
    """Claim, publish, and finalize one batch with at-least-once semantics."""

    if publish_timeout_seconds <= 0:
        raise ValueError("publish_timeout_seconds must be positive")
    if lease_seconds <= publish_timeout_seconds:
        raise ValueError("lease_seconds must exceed publish_timeout_seconds")
    operation_time = now or utc_now()
    async with factory.begin() as session:
        claimed = await claim_outbox_events(
            session,
            now=operation_time,
            limit=batch_size,
            lease_seconds=lease_seconds,
        )
    if not claimed:
        return RelayResult(claimed=0, published=0, failed=0)

    async def publish(claim: ClaimedOutboxEvent) -> Exception | None:
        try:
            await event_bus.publish(
                event=claim.event,
                idempotency_key=IdempotencyKey(f"event:{claim.event.event_id}"),
                timeout_seconds=publish_timeout_seconds,
            )
        except Exception as error:
            return error
        return None

    # Every leased event starts publication immediately. Sequential provider waits
    # could let later leases expire before their first publish attempt.
    errors = await asyncio.gather(*(publish(claim) for claim in claimed))

    published = 0
    failed = 0
    async with factory.begin() as session:
        for claim, error in zip(claimed, errors, strict=True):
            outcome_time = operation_time if now is not None else utc_now()
            if error is not None:
                error_code = error.kind.value if isinstance(error, ProviderError) else "unexpected"
                recorded = await mark_outbox_failed(
                    session,
                    claim.event.event_id,
                    claim.lock_token,
                    error_code,
                    failed_at=outcome_time,
                )
                failed += int(recorded)
            else:
                recorded = await mark_outbox_published(
                    session,
                    claim.event.event_id,
                    claim.lock_token,
                    published_at=outcome_time,
                )
                published += int(recorded)

    return RelayResult(claimed=len(claimed), published=published, failed=failed)
