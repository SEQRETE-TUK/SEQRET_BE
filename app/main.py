"""FastAPI application factory."""

from fastapi import FastAPI

from app import __version__
from app.api.routes.system import router as system_router
from app.config import AppEnvironment, Settings
from app.runtime import RuntimeKind, create_runtime_context


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an API application with explicit, testable dependencies."""

    runtime_context = create_runtime_context(RuntimeKind.API, settings)
    expose_api_docs = runtime_context.settings.environment is not AppEnvironment.PRODUCTION
    application = FastAPI(
        title=runtime_context.settings.app_name,
        version=__version__,
        debug=runtime_context.settings.debug,
        docs_url="/docs" if expose_api_docs else None,
        redoc_url="/redoc" if expose_api_docs else None,
        openapi_url="/openapi.json" if expose_api_docs else None,
    )
    application.state.runtime_context = runtime_context
    application.include_router(system_router)
    return application
