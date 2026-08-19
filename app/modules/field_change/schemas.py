"""HTTP contracts for field reports and customer-facing change proposals."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.contracts.actor import ParticipantRole
from app.contracts.model import ContractModel
from app.modules.field_change.models import FieldIssueType
from app.modules.scope.models import ChangeRequestStatus
from app.modules.scope.schemas import ScopeContent
from app.modules.scope_review.schemas import (
    QuoteSnapshot,
    ScopeMediaPreview,
    ScopeReviewJobHeader,
)


class FieldChangeRequestModel(ContractModel):
    """Accept ordinary JSON primitives while rejecting unknown command fields."""

    model_config = ConfigDict(strict=False)


class FieldIssueCreate(FieldChangeRequestModel):
    client_reference: UUID
    base_scope_version_id: UUID
    issue_type: FieldIssueType
    title: Annotated[str, Field(min_length=1, max_length=200)]
    description: Annotated[str, Field(min_length=1, max_length=2000)]
    evidence_media_asset_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def require_unique_evidence(self) -> Self:
        if len(self.evidence_media_asset_ids) != len(set(self.evidence_media_asset_ids)):
            raise ValueError("field issue evidence IDs must be unique")
        return self


class FieldIssueStatus(StrEnum):
    OPEN = "open"
    CUSTOMER_REVIEW = "customer_review"
    CLARIFICATION_REQUESTED = "clarification_requested"
    APPROVED = "approved"
    REJECTED = "rejected"


class FieldIssueResponse(ContractModel):
    field_issue_id: UUID
    client_reference: UUID
    job_id: UUID
    base_scope_version_id: UUID
    issue_type: FieldIssueType
    title: str
    description: str
    evidence_media_asset_ids: tuple[UUID, ...]
    reported_by_participant_id: UUID
    reported_by_role: ParticipantRole
    reported_at: datetime
    status: FieldIssueStatus
    change_proposal_id: UUID | None


class FieldIssueEvidenceReadResponse(ContractModel):
    """One short-lived, generation-pinned field-issue evidence preview."""

    media_asset_id: UUID
    room_zone_id: UUID
    content_type: str
    read_url: Annotated[
        str,
        Field(min_length=1, repr=False, json_schema_extra={"format": "uri"}),
    ]
    expires_at: datetime


class ChangeProposalCreate(FieldChangeRequestModel):
    field_issue_id: UUID
    base_scope_version_id: UUID
    title: Annotated[str, Field(min_length=1, max_length=200)]
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    proposed_content: ScopeContent
    quote: QuoteSnapshot


class ChangeProposalResponse(ContractModel):
    proposal_id: UUID
    field_issue_id: UUID
    status: ChangeRequestStatus
    base_scope_version_id: UUID
    result_scope_version_id: UUID | None
    quote: QuoteSnapshot
    requested_at: datetime


class ChangeProposalActor(ContractModel):
    participant_id: UUID
    display_name: str
    role: ParticipantRole


class ChangeProposalView(ContractModel):
    job: ScopeReviewJobHeader
    proposal_id: UUID
    field_issue_id: UUID
    status: ChangeRequestStatus
    title: str
    reason: str
    base_scope_version_id: UUID
    base_scope_version_label: str
    result_scope_version_id: UUID | None
    evidence_media: tuple[ScopeMediaPreview, ...]
    quote: QuoteSnapshot
    requested_by: ChangeProposalActor
    requested_at: datetime
    clarification_note: str | None
    clarification_requested_at: datetime | None
    explanation: str | None
    explained_at: datetime | None
    decided_by: ChangeProposalActor | None
    decided_at: datetime | None
    decision_note: str | None


class ChangeProposalDecisionCreate(FieldChangeRequestModel):
    decision: Literal["approve", "reject", "request_clarification"]
    note: Annotated[str, Field(min_length=1, max_length=2000)] | None = None

    @model_validator(mode="after")
    def require_note_for_nonapproval(self) -> Self:
        if self.decision != "approve" and self.note is None:
            raise ValueError("rejection and clarification require a note")
        return self


class ChangeProposalDecisionResponse(ContractModel):
    proposal_id: UUID
    status: ChangeRequestStatus
    result_scope_version_id: UUID | None
    clarification_requested_at: datetime | None
    decided_at: datetime | None


class ChangeProposalExplanationCreate(FieldChangeRequestModel):
    explanation: Annotated[str, Field(min_length=1, max_length=2000)]
