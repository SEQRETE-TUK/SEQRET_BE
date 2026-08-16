"""HTTP contracts for scope review, quote, revision, and confirmation."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.contracts.actor import ParticipantRole
from app.contracts.model import ContractModel
from app.modules.scope.schemas import (
    ScopeContent,
    ScopeItemReviewStatus,
    ScopeItemSource,
)
from app.modules.scope_review.models import ScopeProposalKind, ScopeProposalStatus

MAX_AMOUNT_KRW = 100_000_000_000
ClassificationLabel = Annotated[str, Field(min_length=1, max_length=200)]


class ScopeReviewRequestModel(ContractModel):
    """Accept ordinary JSON primitives while rejecting unknown command fields."""

    model_config = ConfigDict(strict=False)


class QuoteAdjustment(ScopeReviewRequestModel):
    label: Annotated[str, Field(min_length=1, max_length=200)]
    amount_krw: Annotated[int, Field(ge=-MAX_AMOUNT_KRW, le=MAX_AMOUNT_KRW)]


class QuoteSnapshot(ScopeReviewRequestModel):
    base_amount_krw: Annotated[int, Field(ge=0, le=MAX_AMOUNT_KRW)]
    adjustments: Annotated[tuple[QuoteAdjustment, ...], Field(max_length=100)] = ()
    total_amount_krw: Annotated[int, Field(ge=0, le=MAX_AMOUNT_KRW)]

    @model_validator(mode="after")
    def require_exact_total_and_unique_labels(self) -> Self:
        labels = [adjustment.label for adjustment in self.adjustments]
        if len(labels) != len(set(labels)):
            raise ValueError("quote adjustment labels must be unique")
        if (
            self.base_amount_krw + sum(adjustment.amount_krw for adjustment in self.adjustments)
            != self.total_amount_krw
        ):
            raise ValueError("quote total must equal base plus adjustments")
        return self


class ScopeProposalCreate(ScopeReviewRequestModel):
    """Send one company quote based on the exact current scope version."""

    source_scope_version_id: UUID
    content: ScopeContent
    quote: QuoteSnapshot
    included_works: Annotated[tuple[ClassificationLabel, ...], Field(max_length=100)] = ()
    exclusions: Annotated[tuple[ClassificationLabel, ...], Field(max_length=100)] = ()
    reason: Annotated[str, Field(min_length=1, max_length=2000)]

    @model_validator(mode="after")
    def require_unique_classifications(self) -> Self:
        if len(self.included_works) != len(set(self.included_works)):
            raise ValueError("included works must be unique")
        if len(self.exclusions) != len(set(self.exclusions)):
            raise ValueError("exclusions must be unique")
        if set(self.included_works) & set(self.exclusions):
            raise ValueError("included works and exclusions must not overlap")
        return self


class ScopeProposalResponse(ContractModel):
    proposal_id: UUID
    proposal_kind: ScopeProposalKind
    status: ScopeProposalStatus
    source_scope_version_id: UUID
    result_scope_version_id: UUID
    quote: QuoteSnapshot
    included_works: tuple[str, ...]
    exclusions: tuple[str, ...]
    reason: str
    sent_at: datetime
    confirmed_at: datetime | None


class ScopeRevisionRequestCreate(ScopeReviewRequestModel):
    scope_version_id: UUID
    reason: Annotated[str, Field(min_length=1, max_length=2000)]


class ScopeRevisionRequestResponse(ContractModel):
    revision_request_id: UUID
    scope_proposal_id: UUID
    scope_version_id: UUID
    status: Literal["requested", "resolved"]
    reason: str
    requested_at: datetime
    resolved_by_scope_proposal_id: UUID | None
    resolved_at: datetime | None


class ScopeConfirmCreate(ScopeReviewRequestModel):
    scope_version_id: UUID


class ScopeConfirmResponse(ContractModel):
    proposal_id: UUID
    scope_version_id: UUID
    status: Literal["confirmed"] = "confirmed"
    confirmed_at: datetime


class ScopeReviewStatus(StrEnum):
    COMPANY_REVIEW = "company_review"
    CUSTOMER_REVIEW = "customer_review"
    REVISION_REQUESTED = "revision_requested"
    CONFIRMED = "confirmed"


class ScopeReviewJobHeader(ContractModel):
    job_id: UUID
    job_code: str
    title: str
    scheduled_at: datetime | None
    customer_display_name: str | None
    company_display_name: str | None
    viewer_display_name: str
    viewer_role: ParticipantRole
    origin_summary: str | None
    destination_summary: str | None


class ScopeReviewItem(ContractModel):
    item_key: str
    room_zone_id: UUID
    description: str
    name: str
    quantity: int | None
    unit: str | None
    work_note: str | None
    review_status: ScopeItemReviewStatus
    source: ScopeItemSource
    review_required: bool
    source_media_asset_ids: tuple[UUID, ...]


class RoomScopeGroup(ContractModel):
    room_zone_id: UUID
    label: str
    item_count: int
    review_required_count: int
    items: tuple[ScopeReviewItem, ...]


class ScopeMediaPreview(ContractModel):
    media_asset_id: UUID
    room_zone_id: UUID
    content_type: str
    read_url: Annotated[
        str,
        Field(min_length=1, repr=False, json_schema_extra={"format": "uri"}),
    ]
    expires_at: datetime


class ScopeReviewScope(ContractModel):
    id: UUID
    version_label: str
    status: ScopeReviewStatus
    item_count: int
    work_count: int
    exclusion_count: int
    review_required_count: int
    room_groups: tuple[RoomScopeGroup, ...]
    included_works: tuple[str, ...]
    exclusions: tuple[str, ...]


class ScopeReviewView(ContractModel):
    job: ScopeReviewJobHeader
    scope: ScopeReviewScope
    proposal_id: UUID | None
    quote: QuoteSnapshot | None
    proposal_reason: str | None
    media_previews: tuple[ScopeMediaPreview, ...]
    company_confirmed_at: datetime | None
    customer_confirmed_at: datetime | None
    revision_request: ScopeRevisionRequestResponse | None
