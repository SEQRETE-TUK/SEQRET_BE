"""Participant notification responses."""

from datetime import datetime
from uuid import UUID

from app.contracts.events import DomainEventType
from app.contracts.model import ContractModel
from app.modules.notification.models import NotificationChannel, NotificationStatus


class NotificationResponse(ContractModel):
    id: UUID
    event_id: UUID
    event_type: DomainEventType
    job_id: UUID
    recipient_participant_id: UUID
    channel: NotificationChannel
    status: NotificationStatus
    attempt_count: int
    created_at: datetime
    last_attempt_at: datetime | None
    sent_at: datetime | None
    last_error_code: str | None
    provider_message_id: str | None
