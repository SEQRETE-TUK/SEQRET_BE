"""Provider-neutral external notification contracts."""

from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import Field

from app.contracts.model import ContractModel


class ExternalNotificationChannel(StrEnum):
    EMAIL = "email"
    SMS = "sms"
    KAKAO = "kakao"


class OutboundNotification(ContractModel):
    """One sanitized, recipient-specific transactional message."""

    notification_id: UUID
    event_id: UUID
    job_id: UUID
    channel: ExternalNotificationChannel
    destination: Annotated[str, Field(min_length=3, max_length=320, repr=False)]
    subject: Annotated[str, Field(min_length=1, max_length=200)]
    body: Annotated[str, Field(min_length=1, max_length=2000)]
    deep_link: Annotated[str, Field(min_length=1, max_length=2000)]


class NotificationSendResult(ContractModel):
    provider_message_id: Annotated[str, Field(min_length=1, max_length=255)]
