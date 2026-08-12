"""Versioned media-maintenance contracts shared across track boundaries."""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from app.contracts.model import ContractModel
from app.contracts.ports import ProviderErrorKind
from app.contracts.primitives import BackgroundJobId, JobId, MediaAssetId, TraceId


class BackgroundJobType(StrEnum):
    """A-owned maintenance operations currently supported by B handlers."""

    MEDIA_RETENTION_DELETE = "media_retention_delete"


class MediaDeletionTaskV1(ContractModel):
    """Minimal queue message; provider object details stay in A-owned storage."""

    schema_version: Literal[1] = 1
    background_job_id: BackgroundJobId
    job_type: Literal[BackgroundJobType.MEDIA_RETENTION_DELETE] = (
        BackgroundJobType.MEDIA_RETENTION_DELETE
    )
    attempt_count: Annotated[int, Field(ge=1)]
    trace_id: TraceId


class MediaDeletionWorkV1(ContractModel):
    """Immutable deletion snapshot returned through A's application boundary."""

    schema_version: Literal[1] = 1
    background_job_id: BackgroundJobId
    attempt_count: Annotated[int, Field(ge=1)]
    move_job_id: JobId
    media_asset_id: MediaAssetId
    object_key: Annotated[
        str,
        StringConstraints(min_length=1, max_length=1024),
        Field(repr=False),
    ]
    generation: Annotated[str, StringConstraints(min_length=1, max_length=255)]


class MediaDeletionOutcome(StrEnum):
    """Provider-neutral completion outcome returned by the B-owned handler."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class MediaDeletionResultV1(ContractModel):
    """Attempt-scoped result; raw provider messages never cross this boundary."""

    schema_version: Literal[1] = 1
    background_job_id: BackgroundJobId
    attempt_count: Annotated[int, Field(ge=1)]
    outcome: MediaDeletionOutcome
    error_kind: ProviderErrorKind | None = None

    @model_validator(mode="after")
    def require_failure_error(self) -> Self:
        if (self.outcome is MediaDeletionOutcome.FAILED) != (self.error_kind is not None):
            raise ValueError("failed media deletion results require one provider error kind")
        return self
