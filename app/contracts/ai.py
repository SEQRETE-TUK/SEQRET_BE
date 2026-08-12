"""Versioned AI output that remains an unconfirmed draft."""

from typing import Annotated, Literal

from pydantic import Field

from app.contracts.model import ContractModel
from app.contracts.primitives import AnalysisRunId, CaptureSessionId, MediaAssetId


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
    model_name: Annotated[str, Field(min_length=1, max_length=100)]
    model_version: Annotated[str, Field(min_length=1, max_length=100)]
    prompt_version: Annotated[str, Field(min_length=1, max_length=100)]
