"""Idempotent event consumption and notification delivery state commands."""

import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.actor import ParticipantRole
from app.contracts.events import DomainEvent, DomainEventType
from app.contracts.primitives import utc_now
from app.modules.move_job.models import JobParticipant, MoveJob
from app.modules.notification.models import (
    EventConsumption,
    NotificationDelivery,
    NotificationStatus,
)
from app.modules.notification.schemas import NotificationResponse

NOTIFICATION_CONSUMER_NAME = "participant-notifications.v1"
ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
NOTIFICATION_ROLES = {
    DomainEventType.CAPTURE_SUBMITTED_V1: frozenset(
        {ParticipantRole.CUSTOMER, ParticipantRole.COMPANY_MANAGER}
    ),
    DomainEventType.ANALYSIS_COMPLETED_V1: frozenset(
        {ParticipantRole.CUSTOMER, ParticipantRole.COMPANY_MANAGER}
    ),
    DomainEventType.ANALYSIS_FAILED_V1: frozenset(
        {ParticipantRole.CUSTOMER, ParticipantRole.COMPANY_MANAGER}
    ),
    DomainEventType.SCOPE_LOCKED_V1: frozenset({ParticipantRole.FIELD_WORKER}),
    DomainEventType.CHANGE_REQUESTED_V1: frozenset(
        {ParticipantRole.CUSTOMER, ParticipantRole.COMPANY_MANAGER}
    ),
    DomainEventType.DISPATCH_CONFIRMED_V1: frozenset({ParticipantRole.FIELD_WORKER}),
    DomainEventType.COMPLETION_MEDIA_SUBMITTED_V1: frozenset(
        {ParticipantRole.CUSTOMER, ParticipantRole.COMPANY_MANAGER}
    ),
}
NOTIFICATION_STATUS_TRANSITIONS = {
    NotificationStatus.PENDING: frozenset({NotificationStatus.SENT, NotificationStatus.FAILED}),
    NotificationStatus.FAILED: frozenset(
        {NotificationStatus.PENDING, NotificationStatus.SENT, NotificationStatus.FAILED}
    ),
    NotificationStatus.SENT: frozenset(),
}


class NotificationResourceNotFoundError(LookupError):
    """Raised for a missing job or notification intent."""


class NotificationConflictError(ValueError):
    """Raised for an invalid notification state transition."""


def _response(intent: NotificationDelivery) -> NotificationResponse:
    return NotificationResponse(
        id=intent.id,
        event_id=intent.event_id,
        event_type=intent.event_type,
        job_id=intent.job_id,
        recipient_participant_id=intent.recipient_participant_id,
        status=intent.status,
        attempt_count=intent.attempt_count,
        created_at=intent.created_at,
        last_attempt_at=intent.last_attempt_at,
        sent_at=intent.sent_at,
        last_error_code=intent.last_error_code,
    )


async def consume_notification_event(
    session: AsyncSession,
    event: DomainEvent,
) -> tuple[NotificationResponse, ...]:
    """Create each recipient intent once, using event_id as the receipt key."""

    job_id = UUID(str(event.aggregate_id))
    if await session.get(MoveJob, job_id) is None:
        raise NotificationResourceNotFoundError(job_id)
    try:
        async with session.begin_nested():
            session.add(
                EventConsumption(
                    consumer_name=NOTIFICATION_CONSUMER_NAME,
                    event_id=event.event_id,
                    consumed_at=utc_now(),
                )
            )
            await session.flush()
    except IntegrityError:
        return ()

    recipient_roles = NOTIFICATION_ROLES.get(event.event_type, frozenset())
    if not recipient_roles:
        return ()
    participant_ids = (
        await session.scalars(
            select(JobParticipant.id)
            .where(
                JobParticipant.job_id == job_id,
                JobParticipant.role.in_(recipient_roles),
                JobParticipant.id != event.actor_id,
            )
            .order_by(JobParticipant.id)
        )
    ).all()
    intents = [
        NotificationDelivery(
            event_id=event.event_id,
            event_type=event.event_type,
            job_id=job_id,
            recipient_participant_id=participant_id,
            status=NotificationStatus.PENDING,
        )
        for participant_id in participant_ids
    ]
    session.add_all(intents)
    await session.flush()
    return tuple(_response(intent) for intent in intents)


async def list_notifications(
    session: AsyncSession,
    job_id: UUID,
    recipient_participant_id: UUID,
) -> tuple[NotificationResponse, ...]:
    intents = (
        await session.scalars(
            select(NotificationDelivery)
            .where(
                NotificationDelivery.job_id == job_id,
                NotificationDelivery.recipient_participant_id == recipient_participant_id,
            )
            .order_by(NotificationDelivery.created_at, NotificationDelivery.id)
        )
    ).all()
    return tuple(_response(intent) for intent in intents)


async def _load_notification_for_update(
    session: AsyncSession,
    notification_id: UUID,
) -> NotificationDelivery:
    intent = await session.scalar(
        select(NotificationDelivery)
        .where(NotificationDelivery.id == notification_id)
        .with_for_update()
    )
    if intent is None:
        raise NotificationResourceNotFoundError(notification_id)
    return intent


def _require_notification_transition(
    current: NotificationStatus,
    target: NotificationStatus,
) -> None:
    if target not in NOTIFICATION_STATUS_TRANSITIONS[current]:
        raise NotificationConflictError(f"{current.value}->{target.value}")


async def mark_notification_sent(
    session: AsyncSession,
    notification_id: UUID,
) -> NotificationResponse:
    """Record a successful provider delivery without storing provider payloads."""

    intent = await _load_notification_for_update(session, notification_id)
    if intent.status is NotificationStatus.SENT:
        return _response(intent)
    _require_notification_transition(intent.status, NotificationStatus.SENT)
    now = utc_now()
    intent.status = NotificationStatus.SENT
    intent.attempt_count += 1
    intent.last_attempt_at = now
    intent.sent_at = now
    intent.last_error_code = None
    await session.flush()
    await session.refresh(intent)
    return _response(intent)


async def mark_notification_failed(
    session: AsyncSession,
    notification_id: UUID,
    error_code: str,
) -> NotificationResponse:
    """Record a sanitized failure classification for an operator retry."""

    if ERROR_CODE_PATTERN.fullmatch(error_code) is None:
        raise ValueError("error_code must be a lowercase identifier")
    intent = await _load_notification_for_update(session, notification_id)
    _require_notification_transition(intent.status, NotificationStatus.FAILED)
    intent.status = NotificationStatus.FAILED
    intent.attempt_count += 1
    intent.last_attempt_at = utc_now()
    intent.last_error_code = error_code
    await session.flush()
    return _response(intent)


async def retry_notification(
    session: AsyncSession,
    notification_id: UUID,
) -> NotificationResponse:
    """Return a failed intent to the pending queue without erasing attempt history."""

    intent = await _load_notification_for_update(session, notification_id)
    _require_notification_transition(intent.status, NotificationStatus.PENDING)
    intent.status = NotificationStatus.PENDING
    intent.last_error_code = None
    await session.flush()
    return _response(intent)
