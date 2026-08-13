"""Authenticated private Cloud Tasks worker entrypoint."""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Annotated, cast

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import Field, TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.routes.system import router as system_router
from app.config import Settings
from app.contracts.maintenance import MediaDeletionTaskV1, MediaValidationTaskV1
from app.contracts.primitives import utc_now
from app.modules.background_job.service import (
    BackgroundJobNotFoundError,
)
from app.modules.media_processing.deletion import handle_media_deletion
from app.modules.media_processing.validation import handle_media_validation
from app.platform.db import create_database_engine, create_session_factory
from app.platform.http_observability import HttpObservabilityMiddleware
from app.platform.observability import create_observability
from app.platform.storage.gcs import GoogleCloudStorage
from app.runtime import RuntimeKind, create_runtime_context

MediaTask = Annotated[
    MediaValidationTaskV1 | MediaDeletionTaskV1,
    Field(discriminator="job_type"),
]
MEDIA_TASK_ADAPTER: TypeAdapter[MediaTask] = TypeAdapter(MediaTask)


def create_worker_app(settings: Settings | None = None) -> FastAPI:
    """Build the private worker with only task and health routes."""

    runtime_context = create_runtime_context(RuntimeKind.WORKER, settings)
    observability = create_observability(runtime_context)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as resources:
            resources.callback(observability.shutdown)
            if runtime_context.settings.media_bucket_name is None:
                raise ValueError("media_bucket_name is required for the worker")
            engine = create_database_engine(runtime_context.settings)
            resources.push_async_callback(engine.dispose)
            application.state.database_session_factory = create_session_factory(engine)
            application.state.storage_port = GoogleCloudStorage(
                runtime_context.settings.media_bucket_name
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

    application.add_middleware(HttpObservabilityMiddleware, observability=observability)
    return application


app = create_worker_app()
