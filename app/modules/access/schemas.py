"""Secret access-link API schemas."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import Field

from app.contracts.actor import ParticipantRole
from app.contracts.model import ContractModel


class AccessLinkResponse(ContractModel):
    """One-time plaintext credential returned only when issued."""

    id: UUID
    job_id: UUID
    participant_id: UUID
    role: ParticipantRole
    secret: Annotated[str, Field(min_length=40, max_length=100, repr=False)]
    expires_at: datetime
