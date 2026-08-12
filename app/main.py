"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.routes.system import router as system_router
from app.config import AppEnvironment, Settings
from app.modules.access.router import router as access_router
from app.modules.capture.router import router as capture_router
from app.modules.move_job.router import router as move_job_router
from app.platform.db import create_database_engine, create_session_factory
from app.runtime import RuntimeKind, create_runtime_context


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an API application with explicit, testable dependencies."""

    runtime_context = create_runtime_context(RuntimeKind.API, settings)
    expose_api_docs = runtime_context.settings.environment is not AppEnvironment.PRODUCTION

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if runtime_context.settings.database_url is None:
            yield
            return

        engine = create_database_engine(runtime_context.settings)
        app.state.database_session_factory = create_session_factory(engine)
        try:
            yield
        finally:
            await engine.dispose()

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
    application.include_router(system_router)
    application.include_router(move_job_router, prefix=runtime_context.settings.api_prefix)
    application.include_router(access_router, prefix=runtime_context.settings.api_prefix)
    application.include_router(capture_router, prefix=runtime_context.settings.api_prefix)
    return application
