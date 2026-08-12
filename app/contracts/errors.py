"""Stable API error envelope independent of FastAPI internals."""

from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints

from app.contracts.model import ContractModel
from app.contracts.primitives import RequestId


class ErrorDetail(ContractModel):
    """One machine-readable validation or policy failure."""

    field: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    reason: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    context: dict[str, JsonValue] = Field(default_factory=dict)


class ErrorResponse(ContractModel):
    """Versioned external error representation."""

    schema_version: Literal[1] = 1
    code: Annotated[
        str,
        StringConstraints(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_.]*$"),
    ]
    message: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    request_id: RequestId
    details: tuple[ErrorDetail, ...] = ()
