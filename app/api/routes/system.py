"""Runtime health endpoints."""

from typing import Literal, cast

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, ConfigDict

from app.config import AppEnvironment
from app.runtime import RuntimeContext, RuntimeKind

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    """Non-sensitive process health information."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"
    service: str
    environment: AppEnvironment
    runtime: RuntimeKind


@router.get(
    "/healthz",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    summary="프로세스 상태 확인",
)
def healthcheck(request: Request) -> HealthResponse:
    """Confirm that the API process completed application bootstrap."""

    context = cast(RuntimeContext, request.app.state.runtime_context)
    return HealthResponse(
        service=context.service_name,
        environment=context.settings.environment,
        runtime=context.kind,
    )
