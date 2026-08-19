"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes.system import router as system_router
from app.config import AppEnvironment, Settings
from app.modules.access.router import identity_router
from app.modules.access.router import router as access_router
from app.modules.analysis_review.router import router as analysis_review_router
from app.modules.background_job.router import router as background_job_router
from app.modules.capture.router import router as capture_router
from app.modules.completion.router import router as completion_router
from app.modules.dispatch.router import router as dispatch_router
from app.modules.field_change.router import router as field_change_router
from app.modules.move_job.router import router as move_job_router
from app.modules.notification.router import router as notification_router
from app.modules.scope.router import router as scope_router
from app.modules.scope_review.router import router as scope_review_router
from app.platform.cache import create_redis_cache
from app.platform.db import create_database_engine, create_session_factory
from app.platform.http_observability import HttpObservabilityMiddleware
from app.platform.observability import create_observability
from app.platform.storage.gcs import GoogleCloudStorage
from app.runtime import RuntimeKind, create_runtime_context


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an API application with explicit, testable dependencies."""

    runtime_context = create_runtime_context(RuntimeKind.API, settings)
    if (
        runtime_context.settings.environment in {AppEnvironment.STAGING, AppEnvironment.PRODUCTION}
        and runtime_context.settings.frontend_origin is None
    ):
        raise ValueError("frontend_origin is required for a deployed API")
    if runtime_context.settings.environment in {
        AppEnvironment.STAGING,
        AppEnvironment.PRODUCTION,
    } and (
        runtime_context.settings.media_bucket_name is None
        or runtime_context.settings.storage_signing_service_account_email is None
    ):
        raise ValueError("media storage configuration is required for a deployed API")
    observability = create_observability(runtime_context)
    expose_api_docs = runtime_context.settings.environment is not AppEnvironment.PRODUCTION

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as resources:
            resources.callback(observability.shutdown)
            if runtime_context.settings.database_url is not None:
                engine = create_database_engine(runtime_context.settings)
                resources.push_async_callback(engine.dispose)
                app.state.database_session_factory = create_session_factory(engine)
            if runtime_context.settings.redis_url is not None:
                cache = create_redis_cache(runtime_context.settings)
                resources.push_async_callback(cache.close)
                app.state.cache_port = cache
            if runtime_context.settings.media_bucket_name is not None:
                app.state.storage_port = GoogleCloudStorage(
                    runtime_context.settings.media_bucket_name,
                    signing_service_account_email=(
                        runtime_context.settings.storage_signing_service_account_email
                    ),
                )
            yield

    application = FastAPI(
        title=runtime_context.settings.app_name,
        version=__version__,
        debug=runtime_context.settings.debug,
        docs_url="/docs" if expose_api_docs else None,
        redoc_url="/redoc" if expose_api_docs else None,
        openapi_url="/openapi.json" if expose_api_docs else None,
        lifespan=lifespan,
    )
    application.state.runtime_context = runtime_context
    application.state.observability = observability
    application.state.cache_port = None
    application.state.database_session_factory = None
    application.state.storage_port = None
    application.include_router(system_router)
    application.include_router(identity_router, prefix=runtime_context.settings.api_prefix)
    application.include_router(move_job_router, prefix=runtime_context.settings.api_prefix)
    application.include_router(access_router, prefix=runtime_context.settings.api_prefix)
    application.include_router(capture_router, prefix=runtime_context.settings.api_prefix)
    application.include_router(analysis_review_router, prefix=runtime_context.settings.api_prefix)
    application.include_router(scope_router, prefix=runtime_context.settings.api_prefix)
    application.include_router(scope_review_router, prefix=runtime_context.settings.api_prefix)
    application.include_router(field_change_router, prefix=runtime_context.settings.api_prefix)
    application.include_router(dispatch_router, prefix=runtime_context.settings.api_prefix)
    application.include_router(completion_router, prefix=runtime_context.settings.api_prefix)
    application.include_router(notification_router, prefix=runtime_context.settings.api_prefix)
    application.include_router(background_job_router, prefix=runtime_context.settings.api_prefix)
    application.add_middleware(HttpObservabilityMiddleware, observability=observability)
    if runtime_context.settings.frontend_origin is not None:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=[runtime_context.settings.frontend_origin],
            allow_credentials=True,
            allow_methods=["DELETE", "GET", "PATCH", "POST", "PUT"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "X-SEQRET-CSRF",
                "X-SEQRET-Job-ID",
                "traceparent",
            ],
            expose_headers=["Retry-After", "X-Request-ID"],
        )
    return application
