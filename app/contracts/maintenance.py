"""Versioned media background-job contracts shared across track boundaries."""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from app.contracts.model import ContractModel
from app.contracts.ports import ProviderErrorKind, StorageObjectGeneration
from app.contracts.primitives import BackgroundJobId, JobId, MediaAssetId, TraceId


class BackgroundJobType(StrEnum):
    """A-owned background operations executed by B handlers."""

    MEDIA_VALIDATION = "media_validation"
    MEDIA_RETENTION_DELETE = "media_retention_delete"


class MediaValidationTaskV1(ContractModel):
    """Minimal queue message without provider object details."""

    schema_version: Literal[1] = 1
    background_job_id: BackgroundJobId
    job_type: Literal[BackgroundJobType.MEDIA_VALIDATION] = BackgroundJobType.MEDIA_VALIDATION
    attempt_count: Annotated[int, Field(ge=1)]
    trace_id: TraceId


class MediaValidationWorkV1(ContractModel):
    """Generation-pinned input returned through A's application boundary."""

    schema_version: Literal[1] = 1
    background_job_id: BackgroundJobId
    attempt_count: Annotated[int, Field(ge=1)]
    object_key: Annotated[
        str,
        StringConstraints(min_length=1, max_length=1024),
        Field(repr=False),
    ]
    source_generation: StorageObjectGeneration
    expected_content_type: Annotated[
        str,
        StringConstraints(min_length=3, max_length=255, pattern=r"^[^/]+/[^/]+$"),
    ]
    expected_size_bytes: Annotated[int, Field(gt=0)]


class MediaValidationOutcome(StrEnum):
    """Provider-neutral terminal outcome returned by the B-owned handler."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class MediaValidationResultV1(ContractModel):
    """Attempt-scoped validation result without raw provider details."""

    schema_version: Literal[1] = 1
    background_job_id: BackgroundJobId
    attempt_count: Annotated[int, Field(ge=1)]
    source_generation: StorageObjectGeneration
    outcome: MediaValidationOutcome
    observed_content_type: (
        Annotated[
            str,
            StringConstraints(min_length=3, max_length=255, pattern=r"^[^/]+/[^/]+$"),
        ]
        | None
    ) = None
    observed_size_bytes: Annotated[int, Field(gt=0)] | None = None
    sha256_hex: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None
    error_kind: ProviderErrorKind | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_padded_provider_values(cls, values: object) -> object:
        if not isinstance(values, dict):
            return values
        for field in ("source_generation", "observed_content_type", "sha256_hex"):
            value = values.get(field)
            if isinstance(value, str) and value != value.strip():
                raise ValueError(f"{field} must not contain surrounding whitespace")
        return values

    @model_validator(mode="after")
    def require_one_terminal_shape(self) -> Self:
        observed = (
            self.observed_content_type,
            self.observed_size_bytes,
            self.sha256_hex,
        )
        if self.outcome is MediaValidationOutcome.SUCCEEDED:
            if self.error_kind is not None or any(value is None for value in observed):
                raise ValueError(
                    "successful media validation results require observed metadata and no error"
                )
        elif self.error_kind is None or any(value is not None for value in observed):
            raise ValueError(
                "failed media validation results require one error and no observed metadata"
            )
        return self


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
