"""Runtime health endpoints."""

import os
from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    revision: str | None = None


def _health_response(request: Request, response: Response) -> HealthResponse:
    context = cast(RuntimeContext, request.app.state.runtime_context)
    response.headers["Cache-Control"] = "no-store"
    return HealthResponse(
        service=context.service_name,
        environment=context.settings.environment,
        runtime=context.kind,
        revision=os.getenv("K_REVISION"),
    )


@router.get(
    "/healthz",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    summary="프로세스 상태 확인",
)
def healthcheck(request: Request, response: Response) -> HealthResponse:
    """Confirm that the API process completed application bootstrap."""

    return _health_response(request, response)


@router.get(
    "/edgez",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    summary="공개 경로 상태 확인",
)
def edgecheck(request: Request, response: Response) -> HealthResponse:
    """Confirm that ordinary public traffic reaches the API process."""

    return _health_response(request, response)


@router.get(
    "/readyz",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    summary="데이터베이스 연결 준비 상태 확인",
)
async def readiness(request: Request, response: Response) -> HealthResponse:
    """Confirm that the process can reach the deployment database."""

    factory = cast(
        async_sessionmaker[AsyncSession] | None,
        getattr(request.app.state, "database_session_factory", None),
    )
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="service is not ready",
        )
    try:
        async with factory() as session:
            await session.scalar(text("SELECT 1"))
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="service is not ready",
        ) from error
    return _health_response(request, response)
