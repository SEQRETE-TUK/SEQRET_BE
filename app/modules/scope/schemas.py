"""Work-scope content and HTTP schemas."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.contracts.actor import ParticipantRole
from app.contracts.ai import AnalysisResult
from app.contracts.model import ContractModel
from app.modules.scope.models import ChangeRequestStatus


class ScopeRequestModel(ContractModel):
    model_config = ConfigDict(strict=False)


class ScopeItem(ScopeRequestModel):
    item_key: Annotated[str, Field(min_length=1, max_length=100)]
    room_zone_id: UUID
    description: Annotated[str, Field(min_length=1, max_length=2000)]


class ScopeItemReviewStatus(StrEnum):
    CONFIRMED = "confirmed"
    REVIEW_REQUIRED = "review_required"


class ScopeItemSource(StrEnum):
    AI = "ai"
    CUSTOMER = "customer"
    COMPANY = "company"
    FIELD_CHANGE = "field_change"


class ScopeItemV2(ScopeRequestModel):
    """Structured inventory line preserved inside an immutable scope snapshot."""

    item_key: Annotated[str, Field(min_length=1, max_length=100)]
    room_zone_id: UUID
    name: Annotated[str, Field(min_length=1, max_length=200)]
    quantity: Annotated[int, Field(ge=1)] | None = None
    unit: Annotated[str, Field(min_length=1, max_length=20)] | None = None
    work_note: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    review_status: ScopeItemReviewStatus
    source: ScopeItemSource

    @model_validator(mode="after")
    def require_complete_confirmed_quantity(self) -> "ScopeItemV2":
        if self.review_status is ScopeItemReviewStatus.CONFIRMED and (
            self.quantity is None or self.unit is None
        ):
            raise ValueError("confirmed scope items require quantity and unit")
        return self


class ScopeContent(ScopeRequestModel):
    schema_version: Literal[1, 2] = 1
    items: Annotated[
        tuple[ScopeItem | ScopeItemV2, ...],
        Field(min_length=1, max_length=500),
    ]

    @model_validator(mode="after")
    def require_version_shape_and_unique_item_keys(self) -> "ScopeContent":
        expected_type = ScopeItem if self.schema_version == 1 else ScopeItemV2
        if any(not isinstance(item, expected_type) for item in self.items):
            raise ValueError("scope item shape must match schema version")
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
    created_by_participant_id: UUID | None
    created_at: datetime
    approval_roles: tuple[ParticipantRole, ...]
    locked_at: datetime | None
    analysis_source: AnalysisResult | None


class ScopeApprovalResponse(ContractModel):
    id: UUID
    scope_version_id: UUID
    participant_id: UUID
    role: ParticipantRole
    approved_at: datetime


class ScopeApprovalResult(ContractModel):
    approval: ScopeApprovalResponse
    version: ScopeVersionResponse


class ChangeRequestCreate(ScopeRequestModel):
    base_scope_version_id: UUID
    description: Annotated[str, Field(min_length=1, max_length=2000)]
    proposed_content: ScopeContent
    evidence_media_asset_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def require_unique_evidence(self) -> "ChangeRequestCreate":
        if len(self.evidence_media_asset_ids) != len(set(self.evidence_media_asset_ids)):
            raise ValueError("change evidence IDs must be unique")
        return self


class ChangeClarificationCreate(ScopeRequestModel):
    message: Annotated[str, Field(min_length=1, max_length=2000)]


class ChangeExplanationCreate(ScopeRequestModel):
    explanation: Annotated[str, Field(min_length=1, max_length=2000)]


class ChangeDecisionCreate(ScopeRequestModel):
    decision: Literal["approve", "reject"]
    note: Annotated[str, Field(min_length=1, max_length=2000)] | None = None

    @model_validator(mode="after")
    def require_rejection_note(self) -> "ChangeDecisionCreate":
        if self.decision == "reject" and self.note is None:
            raise ValueError("rejection requires a note")
        return self


class ChangeRequestResponse(ContractModel):
    id: UUID
    job_id: UUID
    base_scope_version_id: UUID
    requested_by_participant_id: UUID
    description: str
    proposed_content: ScopeContent
    evidence_media_asset_ids: tuple[UUID, ...]
    status: ChangeRequestStatus
    clarification_requested_by_participant_id: UUID | None
    clarification_request: str | None
    clarification_requested_at: datetime | None
    explanation: str | None
    explained_at: datetime | None
    decided_by_participant_id: UUID | None
    decision_note: str | None
    decided_at: datetime | None
    result_scope_version_id: UUID | None
    created_at: datetime


class ChangeEvidenceReadResponse(ContractModel):
    media_asset_id: UUID
    read_url: Annotated[
        str,
        Field(min_length=1, repr=False, json_schema_extra={"format": "uri"}),
    ]
    expires_at: datetime
