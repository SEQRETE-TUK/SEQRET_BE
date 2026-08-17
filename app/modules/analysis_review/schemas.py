"""Provider-neutral HTTP schemas for customer AI-draft review."""

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.contracts.ai import DraftLocationCondition
from app.contracts.model import ContractModel
from app.modules.scope.schemas import (
    ScopeItemReviewStatus,
    ScopeItemSource,
    ScopeLocationConditions,
)


class AnalysisReviewRequestModel(ContractModel):
    """Accept ordinary JSON values while keeping response contracts strict."""

    model_config = ConfigDict(strict=False)


class AnalysisReviewItemInput(AnalysisReviewRequestModel):
    """One customer-reviewed scope item."""

    item_key: Annotated[str, Field(min_length=1, max_length=100)]
    room_zone_id: UUID
    description: Annotated[str, Field(min_length=1, max_length=2000)] | None = None
    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    quantity: Annotated[int, Field(ge=1)] | None = None
    unit: Annotated[str, Field(min_length=1, max_length=20)] | None = None
    work_note: Annotated[str, Field(min_length=1, max_length=500)] | None = None

    @model_validator(mode="after")
    def require_one_supported_item_shape(self) -> Self:
        is_v1 = self.description is not None
        is_v2 = self.name is not None or self.quantity is not None or self.unit is not None
        if is_v1 == is_v2:
            raise ValueError("review item must use exactly one schema shape")
        if is_v2 and (self.name is None or self.quantity is None or self.unit is None):
            raise ValueError("structured review items require name, quantity, and unit")
        if is_v1 and self.work_note is not None:
            raise ValueError("legacy review items cannot include work note")
        return self


class AnalysisReviewComplete(AnalysisReviewRequestModel):
    """Atomic final contents based on exactly one AI draft version."""

    source_scope_version_id: UUID
    scope_schema_version: Literal[1, 2] = 1
    items: Annotated[
        tuple[AnalysisReviewItemInput, ...],
        Field(min_length=1, max_length=500),
    ]
    location_conditions: tuple[ScopeLocationConditions, ...] = ()

    @model_validator(mode="after")
    def require_unique_item_keys(self) -> Self:
        expected_v2 = self.scope_schema_version == 2
        if any((item.name is not None) != expected_v2 for item in self.items):
            raise ValueError("review item shape must match scope schema version")
        item_keys = [item.item_key for item in self.items]
        if len(item_keys) != len(set(item_keys)):
            raise ValueError("analysis review item keys must be unique")
        if self.scope_schema_version == 1 and self.location_conditions:
            raise ValueError("analysis review v1 cannot contain location conditions")
        location_ids = [item.location_id for item in self.location_conditions]
        location_kinds = [item.kind for item in self.location_conditions]
        if len(location_ids) != len(set(location_ids)):
            raise ValueError("analysis review location IDs must be unique")
        if len(location_kinds) != len(set(location_kinds)):
            raise ValueError("analysis review location kinds must be unique")
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
    name: str
    quantity: int | None
    unit: str | None
    work_note: str | None
    review_status: ScopeItemReviewStatus
    scope_source: ScopeItemSource
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
    scope_schema_version: Literal[1, 2]
    analysis_completed_at: datetime
    review_completed_at: datetime | None
    zones: tuple[AnalysisReviewZone, ...]
    items: tuple[AnalysisReviewItem, ...]
    location_conditions: tuple[ScopeLocationConditions, ...]
    location_condition_suggestions: tuple[DraftLocationCondition, ...]
