"""Versioned AI output that remains an unconfirmed draft."""

from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.contracts.model import ContractModel
from app.contracts.primitives import AnalysisRunId, CaptureSessionId, MediaAssetId, TraceId

AnalysisContentType = Literal["image/jpeg", "image/png", "video/mp4"]
AnalysisLocationKind = Literal["origin", "destination"]
AnalysisKnowledgeStatus = Literal["known", "unknown"]
AnalysisResidenceType = Literal[
    "apartment",
    "villa",
    "officetel",
    "house",
    "studio",
    "other",
    "unknown",
]
AnalysisElevatorAvailability = Literal["available", "unavailable", "unknown"]
AnalysisStairUsage = Literal["required", "not_required", "unknown"]
AnalysisParkingAccess = Literal["available", "restricted", "unavailable", "unknown"]
AnalysisLocationConditionField = Literal[
    "residence_type",
    "floor",
    "elevator",
    "stairs",
    "parking_access",
    "carry_distance",
    "access_note",
]


class AnalysisFailureStage(StrEnum):
    """Stable stage where one capture analysis stopped."""

    PROMPT = "prompt"
    INPUT_LOOKUP = "input_lookup"
    PROVIDER_CALL = "provider_call"
    PARSE = "parse"
    SOURCE_MAP = "source_map"
    RESULT_LOAD = "result_load"
    SCOPE_IMPORT = "scope_import"


class AnalysisFailureDetail(StrEnum):
    """Safe diagnostic detail that never contains provider or media contents."""

    PROMPT_NOT_CONFIGURED = "prompt_not_configured"
    NO_READY_MEDIA = "no_ready_media"
    PROVIDER_REJECTED = "provider_rejected"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    EMPTY_RESPONSE = "empty_response"
    SCHEMA_VALIDATION = "schema_validation"
    INVALID_SOURCE_REFERENCE = "invalid_source_reference"
    MIXED_SOURCE_LOCATION = "mixed_source_location"
    DUPLICATE_ITEM_KEY = "duplicate_item_key"
    DUPLICATE_LOCATION = "duplicate_location"
    RESULT_MISSING = "result_missing"
    SCOPE_IMPORT_INVALID = "scope_import_invalid"


class AnalysisSourceContext(ContractModel):
    """Non-address topology attached to one approved analysis source."""

    media_asset_id: MediaAssetId
    location_id: UUID
    location_kind: AnalysisLocationKind
    room_zone_id: UUID


class AnalysisFloorCondition(ContractModel):
    """AI-readable floor value with an explicit unknown state."""

    status: AnalysisKnowledgeStatus = "unknown"
    value: Annotated[int, Field(ge=-10, le=200)] | None = None

    @model_validator(mode="after")
    def require_value_to_match_status(self) -> Self:
        if (self.status == "known") != (self.value is not None):
            raise ValueError("known floor requires a value and unknown floor forbids one")
        return self


class AnalysisCarryDistanceCondition(ContractModel):
    """AI-readable carry distance with an explicit unknown state."""

    status: AnalysisKnowledgeStatus = "unknown"
    value_m: Annotated[int, Field(ge=0, le=100_000)] | None = None

    @model_validator(mode="after")
    def require_value_to_match_status(self) -> Self:
        if (self.status == "known") != (self.value_m is not None):
            raise ValueError(
                "known carry distance requires a value and unknown distance forbids one"
            )
        return self


class DraftLocationCondition(ContractModel):
    """Quote-impacting location-condition suggestion requiring human review."""

    location_id: UUID
    location_kind: AnalysisLocationKind
    residence_type: AnalysisResidenceType = "unknown"
    floor: AnalysisFloorCondition = Field(default_factory=AnalysisFloorCondition)
    elevator: AnalysisElevatorAvailability = "unknown"
    stairs: AnalysisStairUsage = "unknown"
    parking_access: AnalysisParkingAccess = "unknown"
    carry_distance: AnalysisCarryDistanceCondition = Field(
        default_factory=AnalysisCarryDistanceCondition
    )
    access_note: Annotated[str, Field(min_length=1, max_length=1000)] | None = None
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    review_required_fields: tuple[AnalysisLocationConditionField, ...] = ()
    source_media_asset_ids: tuple[MediaAssetId, ...] = ()

    @model_validator(mode="after")
    def require_unique_review_fields_and_sources(self) -> Self:
        if len(set(self.review_required_fields)) != len(self.review_required_fields):
            raise ValueError("location review-required fields must be unique")
        if len(set(self.source_media_asset_ids)) != len(self.source_media_asset_ids):
            raise ValueError("location source media asset IDs must be unique")
        return self


class DraftItem(ContractModel):
    """One versioned suggestion that a human may edit or reject."""

    item_key: Annotated[str, Field(min_length=1, max_length=100)]
    description: Annotated[str, Field(min_length=1, max_length=2000)]
    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    quantity: Annotated[int, Field(ge=1)] | None = None
    unit: Annotated[str, Field(min_length=1, max_length=20)] | None = None
    work_note: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    source_media_asset_ids: tuple[MediaAssetId, ...] = ()


class AnalysisResult(ContractModel):
    """Provider-neutral AI result; never an immutable scope version."""

    analysis_run_id: AnalysisRunId
    capture_session_id: CaptureSessionId
    model_name: Annotated[str, Field(min_length=1, max_length=100)]
    model_version: Annotated[str, Field(min_length=1, max_length=100)]
    prompt_version: Annotated[str, Field(min_length=1, max_length=100)]
    result_schema_version: Literal[1, 2] = 1
    draft_items: tuple[DraftItem, ...]
    review_required_items: tuple[DraftItem, ...] = ()
    location_condition_suggestions: tuple[DraftLocationCondition, ...] = ()

    @model_validator(mode="after")
    def require_result_shape_to_match_version(self) -> Self:
        items = self.draft_items + self.review_required_items
        if self.result_schema_version == 1:
            if self.location_condition_suggestions:
                raise ValueError("analysis result v1 cannot contain location conditions")
            if any(
                item.name is not None
                or item.quantity is not None
                or item.unit is not None
                or item.work_note is not None
                for item in items
            ):
                raise ValueError("analysis result v1 cannot contain structured item fields")
            return self

        if any(item.name is None for item in items):
            raise ValueError("analysis result v2 items require name")
        if any(item.quantity is None or item.unit is None for item in self.draft_items):
            raise ValueError("analysis result v2 draft items require quantity and unit")
        if any(
            (item.quantity is None) != (item.unit is None) for item in self.review_required_items
        ):
            raise ValueError(
                "analysis result v2 review-required quantity and unit must be present together"
            )
        item_keys = [item.item_key for item in items]
        if len(set(item_keys)) != len(item_keys):
            raise ValueError("analysis result v2 item keys must be unique")
        if any(
            not item.source_media_asset_ids
            or len(set(item.source_media_asset_ids)) != len(item.source_media_asset_ids)
            for item in items
        ):
            raise ValueError("analysis result v2 items require unique source media asset IDs")
        location_ids = [item.location_id for item in self.location_condition_suggestions]
        location_kinds = [item.location_kind for item in self.location_condition_suggestions]
        if len(set(location_ids)) != len(location_ids) or len(set(location_kinds)) != len(
            location_kinds
        ):
            raise ValueError("analysis result v2 location suggestions must be unique")
        if any(not item.source_media_asset_ids for item in self.location_condition_suggestions):
            raise ValueError("location suggestions require source media asset IDs")
        return self


class AnalysisRequest(ContractModel):
    """Provider-neutral analysis input composed only from A-approved media."""

    analysis_run_id: AnalysisRunId
    capture_session_id: CaptureSessionId
    source_media_asset_ids: Annotated[
        tuple[MediaAssetId, ...],
        Field(min_length=1),
    ]
    object_keys: Annotated[tuple[str, ...], Field(min_length=1)]
    content_types: Annotated[
        tuple[AnalysisContentType, ...],
        Field(min_length=1),
    ]
    model_name: Annotated[str, Field(min_length=1, max_length=100)]
    model_version: Annotated[str, Field(min_length=1, max_length=100)]
    prompt_version: Annotated[str, Field(min_length=1, max_length=100)]
    requested_result_schema_version: Literal[1, 2] = 1
    source_contexts: tuple[AnalysisSourceContext, ...] = ()

    @model_validator(mode="after")
    def require_unique_one_to_one_sources(self) -> Self:
        if (
            len({len(self.source_media_asset_ids), len(self.object_keys), len(self.content_types)})
            != 1
        ):
            raise ValueError(
                "source media asset IDs, object keys, and content types must have the same length"
            )
        if len(set(self.source_media_asset_ids)) != len(self.source_media_asset_ids):
            raise ValueError("source media asset IDs must be unique")
        if len(set(self.object_keys)) != len(self.object_keys):
            raise ValueError("object keys must be unique")
        if self.source_contexts and tuple(
            context.media_asset_id for context in self.source_contexts
        ) != tuple(self.source_media_asset_ids):
            raise ValueError("source contexts must match source media asset IDs in order")
        if self.requested_result_schema_version == 2 and not self.source_contexts:
            raise ValueError("analysis request v2 requires source contexts")
        return self


class AnalysisTaskV1(ContractModel):
    """Minimal analysis queue message; media details are looked up by the worker."""

    schema_version: Literal[1] = 1
    analysis_run_id: AnalysisRunId
    capture_session_id: CaptureSessionId
    attempt_count: Annotated[int, Field(ge=1)]
    trace_id: TraceId
