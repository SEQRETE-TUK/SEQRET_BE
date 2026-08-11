"""Tests for the FastAPI application factory."""

from fastapi.testclient import TestClient

from app.config import AppEnvironment, Settings
from app.entrypoints.api import app as api_entrypoint
from app.main import create_app
from app.runtime import RuntimeKind


def test_asgi_entrypoint_exposes_api_runtime() -> None:
    assert api_entrypoint.state.runtime_context.kind is RuntimeKind.API


def test_application_factory_uses_injected_settings() -> None:
    settings = Settings(
        app_name="SEQRET Test API",
        environment=AppEnvironment.TEST,
        debug=True,
    )

    application = create_app(settings)

    assert application.title == "SEQRET Test API"
    assert application.debug is True
    assert application.state.runtime_context.kind is RuntimeKind.API
    assert application.state.runtime_context.settings is settings


def test_healthcheck_reports_runtime_without_secrets() -> None:
    settings = Settings(environment=AppEnvironment.TEST)

    with TestClient(create_app(settings)) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "seqret-api",
        "environment": "test",
        "runtime": "api",
    }
