"""Authenticated private Cloud Tasks worker entrypoint."""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Annotated, cast

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import Field, TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.routes.system import router as system_router
from app.config import Settings
from app.contracts.ai import AnalysisTaskV1
from app.contracts.maintenance import MediaDeletionTaskV1, MediaValidationTaskV1
from app.contracts.ports import AIProviderPort, ProviderErrorKind
from app.contracts.primitives import utc_now
from app.modules.analysis.handler import AnalysisTaskStatus, handle_analysis_task
from app.modules.analysis.orchestration import (
    AnalysisInputsUnavailableError,
    build_analysis_request,
)
from app.modules.analysis.service import load_analysis_result
from app.modules.analysis_workflow.service import (
    CaptureAnalysisNotFoundError,
    complete_capture_analysis,
    fail_capture_analysis,
    start_capture_analysis,
)
from app.modules.background_job.service import (
    BackgroundJobNotFoundError,
)
from app.modules.media_processing.deletion import handle_media_deletion
from app.modules.media_processing.validation import handle_media_validation
from app.platform.ai.vertex import VertexAIProvider
from app.platform.db import (
    create_database_engine,
    create_session_factory,
    transactional_session,
)
from app.platform.http_observability import HttpObservabilityMiddleware
from app.platform.observability import create_observability
from app.platform.storage.gcs import GoogleCloudStorage
from app.runtime import RuntimeKind, create_runtime_context

MediaTask = Annotated[
    MediaValidationTaskV1 | MediaDeletionTaskV1,
    Field(discriminator="job_type"),
]
MEDIA_TASK_ADAPTER: TypeAdapter[MediaTask] = TypeAdapter(MediaTask)

ANALYSIS_MODEL_NAME = "gemini-2.5-flash"
ANALYSIS_MODEL_VERSION = "2025-08"
ANALYSIS_PROMPT_VERSION = "inventory-1"
ANALYSIS_PROMPT_LIBRARY = {
    ANALYSIS_PROMPT_VERSION: (
        "촬영 영상에서 이동 대상 이삿짐을 방·구역별로 찾아 나열하고 각 항목의 수량과 "
        "확인이 필요한 항목 여부를 표시하라. 가격·차량·인원·파손·책임은 판단하지 마라."
    ),
}


def create_worker_app(settings: Settings | None = None) -> FastAPI:
    """Build the private worker with only task and health routes."""

    runtime_context = create_runtime_context(RuntimeKind.WORKER, settings)
    observability = create_observability(runtime_context)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as resources:
            resources.callback(observability.shutdown)
            settings = runtime_context.settings
            bucket_name = settings.media_bucket_name
            if bucket_name is None:
                raise ValueError("media_bucket_name is required for the worker")
            engine = create_database_engine(settings)
            resources.push_async_callback(engine.dispose)
            application.state.database_session_factory = create_session_factory(engine)
            application.state.storage_port = GoogleCloudStorage(bucket_name)
            if settings.gcp_project_id is not None and settings.analysis_location is not None:
                application.state.ai_provider = VertexAIProvider(
                    project=settings.gcp_project_id,
                    location=settings.analysis_location,
                    bucket_name=bucket_name,
                    prompt_library=ANALYSIS_PROMPT_LIBRARY,
                )
            yield

    application = FastAPI(
        title="SEQRET Private Worker",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.state.runtime_context = runtime_context
    application.state.observability = observability
    application.state.database_session_factory = None
    application.state.storage_port = None
    application.state.ai_provider = None
    application.include_router(system_router)

    @application.post("/tasks/media", status_code=status.HTTP_204_NO_CONTENT)
    async def handle_media_task(request: Request) -> Response:
        try:
            task = MEDIA_TASK_ADAPTER.validate_json(await request.body())
        except ValidationError as error:
            raise RequestValidationError(error.errors()) from None
        factory = cast(
            async_sessionmaker[AsyncSession],
            request.app.state.database_session_factory,
        )
        try:
            if isinstance(task, MediaValidationTaskV1):
                await handle_media_validation(
                    factory,
                    request.app.state.storage_port,
                    task,
                    now=utc_now(),
                )
            else:
                await handle_media_deletion(
                    factory,
                    request.app.state.storage_port,
                    task,
                    now=utc_now(),
                )
        except BackgroundJobNotFoundError:
            # A missing or stale attempt has no provider effect to retry.
            pass
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.post("/tasks/analysis", status_code=status.HTTP_204_NO_CONTENT)
    async def handle_analysis(request: Request) -> Response:
        try:
            task = AnalysisTaskV1.model_validate_json(await request.body())
        except ValidationError as error:
            raise RequestValidationError(error.errors()) from None
        provider = request.app.state.ai_provider
        if provider is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="analysis provider is not configured",
            )
        factory = cast(
            async_sessionmaker[AsyncSession],
            request.app.state.database_session_factory,
        )
        try:
            async with transactional_session(factory) as session:
                should_process = await start_capture_analysis(session, task)
        except CaptureAnalysisNotFoundError:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        if not should_process:
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        try:
            async with factory() as session:
                analysis_request = await build_analysis_request(
                    session,
                    analysis_run_id=task.analysis_run_id,
                    capture_session_id=task.capture_session_id,
                    model_name=ANALYSIS_MODEL_NAME,
                    model_version=ANALYSIS_MODEL_VERSION,
                    prompt_version=ANALYSIS_PROMPT_VERSION,
                )
        except AnalysisInputsUnavailableError:
            async with transactional_session(factory) as write_session:
                await fail_capture_analysis(
                    write_session,
                    task,
                    error_kind=ProviderErrorKind.INVALID_INPUT,
                    retryable=False,
                    completed_at=utc_now(),
                )
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        outcome = await handle_analysis_task(
            factory,
            cast(AIProviderPort, provider),
            analysis_request,
            trace_id=task.trace_id,
            now=utc_now(),
        )
        async with transactional_session(factory) as session:
            if outcome.status is AnalysisTaskStatus.SUCCEEDED:
                result = await load_analysis_result(
                    session,
                    analysis_run_id=task.analysis_run_id,
                )
                if result is None:
                    await fail_capture_analysis(
                        session,
                        task,
                        error_kind=ProviderErrorKind.CONFLICT,
                        retryable=False,
                        completed_at=utc_now(),
                    )
                else:
                    await complete_capture_analysis(
                        session,
                        task,
                        result,
                        completed_at=utc_now(),
                    )
            else:
                await fail_capture_analysis(
                    session,
                    task,
                    error_kind=outcome.error_kind or ProviderErrorKind.CONFLICT,
                    retryable=outcome.retryable,
                    completed_at=utc_now(),
                )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    application.add_middleware(HttpObservabilityMiddleware, observability=observability)
    return application


app = create_worker_app()
