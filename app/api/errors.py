"""Public HTTP error responses and privacy-safe exception handlers."""

from typing import Any, cast

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class HttpExceptionResponse(BaseModel):
    """The string-detail body emitted by the current FastAPI exception handlers."""

    detail: str


ResponseDefinition = dict[str, Any]

_PUBLIC_VALIDATION_ERROR_FIELDS = ("type", "loc", "msg")

_PROTECTED_ERROR_RESPONSES: dict[int, ResponseDefinition] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": HttpExceptionResponse,
        "description": "Bearer access token is missing or invalid",
        "headers": {
            "WWW-Authenticate": {
                "description": "Bearer authentication challenge",
                "schema": {"type": "string", "example": "Bearer"},
            }
        },
    },
    status.HTTP_403_FORBIDDEN: {
        "model": HttpExceptionResponse,
        "description": "The authenticated participant has insufficient role",
    },
    status.HTTP_404_NOT_FOUND: {
        "model": HttpExceptionResponse,
        "description": "The job-scoped resource was not found",
    },
    status.HTTP_409_CONFLICT: {
        "model": HttpExceptionResponse,
        "description": "The resource is not valid for the requested state transition",
    },
    status.HTTP_429_TOO_MANY_REQUESTS: {
        "model": HttpExceptionResponse,
        "description": "The access-link rate limit was exceeded",
        "headers": {
            "Retry-After": {
                "description": "Seconds until another request can be attempted",
                "schema": {"type": "integer", "minimum": 1},
            }
        },
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": HttpExceptionResponse,
        "description": "A required provider or policy is unavailable",
    },
}


def protected_error_responses(*status_codes: int) -> dict[int | str, ResponseDefinition]:
    """Return the common auth failures plus only the route's reachable failures."""

    requested = {status.HTTP_401_UNAUTHORIZED, status.HTTP_429_TOO_MANY_REQUESTS, *status_codes}
    return {code: _PROTECTED_ERROR_RESPONSES[code] for code in sorted(requested)}


async def request_validation_error_response(
    _request: Request,
    error: Exception,
) -> JSONResponse:
    """Return the documented 422 shape without reflecting request values."""

    validation_error = cast(RequestValidationError, error)
    detail = [
        {field: item[field] for field in _PUBLIC_VALIDATION_ERROR_FIELDS if field in item}
        for item in validation_error.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": detail},
    )
