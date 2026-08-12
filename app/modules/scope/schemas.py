"""Work-scope content and HTTP schemas."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.contracts.actor import ParticipantRole
from app.contracts.model import ContractModel


class ScopeRequestModel(ContractModel):
    model_config = ConfigDict(strict=False)


class ScopeItem(ScopeRequestModel):
    item_key: Annotated[str, Field(min_length=1, max_length=100)]
    room_zone_id: UUID
    description: Annotated[str, Field(min_length=1, max_length=2000)]


class ScopeContent(ScopeRequestModel):
    schema_version: Literal[1] = 1
    items: Annotated[tuple[ScopeItem, ...], Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def require_unique_item_keys(self) -> "ScopeContent":
        item_keys = [item.item_key for item in self.items]
        if len(item_keys) != len(set(item_keys)):
            raise ValueError("scope item keys must be unique")
        return self


class ScopeVersionCreate(ScopeRequestModel):
    parent_version_id: UUID | None = None
    content: ScopeContent


class ScopeVersionResponse(ContractModel):
    id: UUID
    job_id: UUID
    parent_version_id: UUID | None
    sequence_number: int
    content: ScopeContent
    content_hash: str
    created_by_participant_id: UUID
    created_at: datetime
    approval_roles: tuple[ParticipantRole, ...]
    locked_at: datetime | None


class ScopeApprovalResponse(ContractModel):
    id: UUID
    scope_version_id: UUID
    participant_id: UUID
    role: ParticipantRole
    approved_at: datetime


class ScopeApprovalResult(ContractModel):
    approval: ScopeApprovalResponse
    version: ScopeVersionResponse
