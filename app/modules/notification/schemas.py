"""Participant notification responses."""

from datetime import datetime
from uuid import UUID

from app.contracts.events import DomainEventType
from app.contracts.model import ContractModel
from app.modules.notification.models import NotificationStatus


class NotificationResponse(ContractModel):
    id: UUID
    event_id: UUID
    event_type: DomainEventType
    job_id: UUID
    recipient_participant_id: UUID
    status: NotificationStatus
    attempt_count: int
    created_at: datetime
    last_attempt_at: datetime | None
    sent_at: datetime | None
    last_error_code: str | None
