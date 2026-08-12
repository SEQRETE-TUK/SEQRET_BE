"""Completion commands and audit timeline responses."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import ConfigDict, Field, JsonValue, model_validator

from app.contracts.actor import ParticipantRole
from app.contracts.model import ContractModel
from app.modules.completion.models import AuditEventType
from app.modules.move_job.models import MoveJobStatus


class CompletionRequestModel(ContractModel):
    model_config = ConfigDict(strict=False)


class CompletionConfirmationCreate(CompletionRequestModel):
    scope_version_id: UUID
    evidence_media_asset_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def require_unique_evidence(self) -> "CompletionConfirmationCreate":
        if len(self.evidence_media_asset_ids) != len(set(self.evidence_media_asset_ids)):
            raise ValueError("completion evidence IDs must be unique")
        return self


class CompletionConfirmationResponse(ContractModel):
    id: UUID
    job_id: UUID
    scope_version_id: UUID
    participant_id: UUID
    role: ParticipantRole
    evidence_media_asset_ids: tuple[UUID, ...]
    confirmed_at: datetime


class CompletionResult(ContractModel):
    confirmation: CompletionConfirmationResponse
    job_status: MoveJobStatus
    completed_at: datetime | None


class AuditEventResponse(ContractModel):
    id: UUID
    job_id: UUID
    event_type: AuditEventType
    actor_participant_id: UUID | None
    payload: dict[str, JsonValue]
    occurred_at: datetime
