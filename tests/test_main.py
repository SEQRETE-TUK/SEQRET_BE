"""Tests for the FastAPI application factory."""

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import StreamingResponse

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
        "revision": None,
    }


def test_healthcheck_reports_cloud_run_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K_REVISION", "seqret-stg-api-00001-abc")

    with TestClient(create_app(Settings(environment=AppEnvironment.TEST))) as client:
        response = client.get("/healthz")

    assert response.json()["revision"] == "seqret-stg-api-00001-abc"


def test_edgecheck_uses_the_ordinary_public_route() -> None:
    with TestClient(create_app(Settings(environment=AppEnvironment.TEST))) as client:
        response = client.get("/edgez")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_readiness_requires_a_working_database_session() -> None:
    application = create_app(Settings(environment=AppEnvironment.TEST))

    with TestClient(application) as client:
        assert client.get("/readyz").status_code == 503

        session = AsyncMock()
        session.scalar.side_effect = SQLAlchemyError("offline")
        session_context = AsyncMock()
        session_context.__aenter__.return_value = session
        application.state.database_session_factory = Mock(return_value=session_context)
        assert client.get("/readyz").status_code == 503

        session.scalar.side_effect = None
        session.scalar.return_value = 1
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_unhandled_error_response_has_request_id() -> None:
    application = create_app(Settings(environment=AppEnvironment.TEST))

    @application.get("/explode")
    async def explode() -> None:
        raise RuntimeError("expected")

    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/explode")

    assert response.status_code == 500
    assert response.headers["x-request-id"]
    assert response.text == "Internal Server Error"


def test_streaming_error_does_not_start_a_second_response() -> None:
    application = create_app(Settings(environment=AppEnvironment.TEST))

    @application.get("/broken-stream")
    async def broken_stream() -> StreamingResponse:
        async def content() -> AsyncIterator[bytes]:
            yield b"started"
            raise RuntimeError("expected")

        return StreamingResponse(content())

    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/broken-stream")

    assert response.status_code == 200
    assert response.headers["x-request-id"]


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


def test_application_lifespan_reuses_and_closes_redis_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = AsyncMock()
    settings = Settings(
        environment=AppEnvironment.TEST,
        redis_url=SecretStr("redis://cache.internal:6379/0"),
    )
    application = create_app(settings)

    monkeypatch.setattr("app.main.create_redis_cache", lambda configured: cache)

    with TestClient(application):
        assert application.state.cache_port is cache

    cache.close.assert_awaited_once_with()
