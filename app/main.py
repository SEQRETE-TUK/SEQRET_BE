"""FastAPI application factory."""

from fastapi import FastAPI

from app import __version__
from app.api.routes.system import router as system_router
from app.config import Settings
from app.runtime import RuntimeKind, create_runtime_context


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an API application with explicit, testable dependencies."""

    runtime_context = create_runtime_context(RuntimeKind.API, settings)
    application = FastAPI(
        title=runtime_context.settings.app_name,
        version=__version__,
        debug=runtime_context.settings.debug,
    )
    application.state.runtime_context = runtime_context
    application.include_router(system_router)
    return application
