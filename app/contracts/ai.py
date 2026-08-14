"""Versioned AI output that remains an unconfirmed draft."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.contracts.model import ContractModel
from app.contracts.primitives import AnalysisRunId, CaptureSessionId, MediaAssetId, TraceId

AnalysisContentType = Literal["image/jpeg", "image/png", "video/mp4"]


class DraftItem(ContractModel):
    """One structured suggestion that a human may edit or reject."""

    item_key: Annotated[str, Field(min_length=1, max_length=100)]
    description: Annotated[str, Field(min_length=1, max_length=2000)]
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    source_media_asset_ids: tuple[MediaAssetId, ...] = ()


class AnalysisResult(ContractModel):
    """Provider-neutral AI result; never an immutable scope version."""

    analysis_run_id: AnalysisRunId
    capture_session_id: CaptureSessionId
    model_name: Annotated[str, Field(min_length=1, max_length=100)]
    model_version: Annotated[str, Field(min_length=1, max_length=100)]
    prompt_version: Annotated[str, Field(min_length=1, max_length=100)]
    result_schema_version: Literal[1] = 1
    draft_items: tuple[DraftItem, ...]
    review_required_items: tuple[DraftItem, ...] = ()


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
        return self


class AnalysisTaskV1(ContractModel):
    """Minimal analysis queue message; media details are looked up by the worker."""

    schema_version: Literal[1] = 1
    analysis_run_id: AnalysisRunId
    capture_session_id: CaptureSessionId
    attempt_count: Annotated[int, Field(ge=1)]
    trace_id: TraceId
