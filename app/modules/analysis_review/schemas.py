"""Provider-neutral HTTP schemas for customer AI-draft review."""

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.contracts.model import ContractModel


class AnalysisReviewRequestModel(ContractModel):
    """Accept ordinary JSON values while keeping response contracts strict."""

    model_config = ConfigDict(strict=False)


class AnalysisReviewItemInput(AnalysisReviewRequestModel):
    """One customer-reviewed scope item."""

    item_key: Annotated[str, Field(min_length=1, max_length=100)]
    room_zone_id: UUID
    description: Annotated[str, Field(min_length=1, max_length=2000)]


class AnalysisReviewComplete(AnalysisReviewRequestModel):
    """Atomic final contents based on exactly one AI draft version."""

    source_scope_version_id: UUID
    items: Annotated[
        tuple[AnalysisReviewItemInput, ...],
        Field(min_length=1, max_length=500),
    ]

    @model_validator(mode="after")
    def require_unique_item_keys(self) -> Self:
        item_keys = [item.item_key for item in self.items]
        if len(item_keys) != len(set(item_keys)):
            raise ValueError("analysis review item keys must be unique")
        return self


class AnalysisReviewZone(ContractModel):
    """Capture validation counts for one origin room zone."""

    room_zone_id: UUID
    name: str
    sort_order: int
    total_media_count: int
    ready_media_count: int
    failed_media_count: int


class AnalysisReviewItem(ContractModel):
    """Current editable item with optional AI provenance."""

    item_key: str
    room_zone_id: UUID
    description: str
    source: Literal["ai", "customer"]
    confidence: float | None
    review_required: bool
    source_media_asset_ids: tuple[UUID, ...]


class AnalysisReviewResponse(ContractModel):
    """Latest completed analysis and its optional customer review."""

    job_id: UUID
    analysis_run_id: UUID
    capture_session_id: UUID
    source_scope_version_id: UUID
    review_scope_version_id: UUID | None
    analysis_completed_at: datetime
    review_completed_at: datetime | None
    zones: tuple[AnalysisReviewZone, ...]
    items: tuple[AnalysisReviewItem, ...]
