"""Secret access-link API schemas."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator

from app.contracts.actor import ParticipantRole
from app.contracts.model import ContractModel
from app.modules.access.models import InvitationStatus


class AccessLinkResponse(ContractModel):
    """One-time plaintext credential returned only when issued."""

    id: UUID
    job_id: UUID
    participant_id: UUID
    role: ParticipantRole
    secret: Annotated[str, Field(min_length=40, max_length=100, repr=False)]
    expires_at: datetime


class InvitationCreate(ContractModel):
    """Request a capability for the next allowed role in the onboarding chain."""

    model_config = ConfigDict(strict=False)

    role: ParticipantRole
    display_name: Annotated[str, Field(min_length=1, max_length=100)] | None = None

    @field_validator("role")
    @classmethod
    def reject_customer_invitation(cls, role: ParticipantRole) -> ParticipantRole:
        if role is ParticipantRole.CUSTOMER:
            raise ValueError("customer cannot be invited")
        return role


class InvitationResponse(ContractModel):
    id: UUID
    job_id: UUID
    issuer_participant_id: UUID
    invitee_participant_id: UUID
    role: ParticipantRole
    display_name: str
    status: InvitationStatus
    issued_at: datetime
    expires_at: datetime
    resolved_at: datetime | None


class InvitationIssuedResponse(ContractModel):
    """Invitation metadata plus one-time plaintext capability."""

    invitation: InvitationResponse
    access_link: AccessLinkResponse


class InvitationListResponse(ContractModel):
    invitations: tuple[InvitationResponse, ...]


class ActorSelfResponse(ContractModel):
    """Bearer capability identity for role-aware landing screens."""

    job_id: UUID
    participant_id: UUID
    role: ParticipantRole
    display_name: str
    permissions: tuple[str, ...]
    expires_at: datetime
    invitation: InvitationResponse | None
