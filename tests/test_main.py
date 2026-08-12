"""Tests for the FastAPI application factory."""

from fastapi.testclient import TestClient
from pydantic import SecretStr

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
    assert application.docs_url == "/docs"
    assert application.redoc_url == "/redoc"
    assert application.openapi_url == "/openapi.json"
    assert application.state.runtime_context.kind is RuntimeKind.API
    assert application.state.runtime_context.settings is settings


def test_healthcheck_reports_runtime_without_secrets() -> None:
    settings = Settings(environment=AppEnvironment.TEST)

    with TestClient(create_app(settings)) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "status": "ok",
        "service": "seqret-api",
        "environment": "test",
        "runtime": "api",
    }


def test_production_application_disables_api_documentation_routes() -> None:
    settings = Settings(environment=AppEnvironment.PRODUCTION)
    application = create_app(settings)

    assert application.docs_url is None
    assert application.redoc_url is None
    assert application.openapi_url is None

    with TestClient(application) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/healthz").status_code == 200


def test_application_lifespan_configures_database_sessions() -> None:
    settings = Settings(
        environment=AppEnvironment.TEST,
        database_url=SecretStr("postgresql+psycopg://seqret:secret@localhost/seqret"),
    )
    application = create_app(settings)

    with TestClient(application):
        assert application.state.database_session_factory is not None
