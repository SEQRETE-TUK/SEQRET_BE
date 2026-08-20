"""Secret access-link API schemas."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.contracts.actor import ParticipantRole
from app.contracts.model import ContractModel
from app.modules.access.models import InvitationStatus, NotificationContactChannel


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


class MoveConnectionCreate(ContractModel):
    """Connect one shared move code as the role selected on the entry screen."""

    model_config = ConfigDict(strict=False)

    connection_code: Annotated[str, Field(min_length=13, max_length=13)]
    role: ParticipantRole

    @field_validator("connection_code")
    @classmethod
    def normalize_connection_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if len(normalized) != 13 or not normalized.startswith("MOVE-"):
            raise ValueError("connection code must use MOVE-XXXXXXXX format")
        try:
            int(normalized[5:], 16)
        except ValueError as error:
            raise ValueError("connection code must use MOVE-XXXXXXXX format") from error
        return normalized


class MoveConnectionPreviewResponse(ContractModel):
    """Display identity resolved without creating a workspace session."""

    role: ParticipantRole
    display_name: str


class WorkspaceMemberResponse(ContractModel):
    """One job role restored from a server-owned workspace session."""

    job_id: UUID
    participant_id: UUID
    role: ParticipantRole
    display_name: str
    invitation: InvitationResponse | None


class WorkspaceSessionResponse(ContractModel):
    """Durable workspace metadata; the HttpOnly cookie is never in the body."""

    account_id: UUID
    role: ParticipantRole
    display_name: str
    expires_at: datetime
    csrf_token: Annotated[str, Field(min_length=40, max_length=100, repr=False)]
    members: tuple[WorkspaceMemberResponse, ...]


class WorkspaceContactPointUpsert(ContractModel):
    """Explicit delivery consent for one external destination."""

    model_config = ConfigDict(strict=False)

    destination: Annotated[str, Field(min_length=3, max_length=320, repr=False)]
    delivery_consent: bool
    enabled: bool = True

    @model_validator(mode="after")
    def require_delivery_consent(self) -> "WorkspaceContactPointUpsert":
        if not self.delivery_consent:
            raise ValueError("delivery_consent must be true")
        return self


class WorkspaceContactPointResponse(ContractModel):
    channel: NotificationContactChannel
    masked_destination: str
    enabled: bool
    consented_at: datetime
    updated_at: datetime


class WorkspaceContactPointListResponse(ContractModel):
    contacts: tuple[WorkspaceContactPointResponse, ...]
