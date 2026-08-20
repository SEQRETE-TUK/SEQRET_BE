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


def test_protected_api_openapi_documents_reachable_http_errors() -> None:
    schema = create_app(Settings(environment=AppEnvironment.TEST)).openapi()
    job_path = "/api/v1/move-jobs/{job_id}"
    route_failures = {
        ("get", "/api/v1/me"): set(),
        ("get", "/api/v1/move-jobs"): set(),
        ("post", "/api/v1/sessions"): {409},
        ("get", job_path): set(),
        ("patch", job_path): {403, 409},
        ("delete", job_path): {403, 409},
        ("post", f"{job_path}/invitations"): {403, 409},
        ("get", f"{job_path}/invitations"): {403},
        ("post", f"{job_path}/invitations/{{invitation_id}}/accept"): {409},
        ("post", f"{job_path}/invitations/{{invitation_id}}/decline"): {409},
        ("post", f"{job_path}/invitations/{{invitation_id}}/revoke"): {403},
        ("post", f"{job_path}/invitations/{{invitation_id}}/reissue"): {403, 409},
        ("post", f"{job_path}/participants/{{participant_id}}/access-links"): {403},
        ("post", f"{job_path}/access-links/{{access_link_id}}/revoke"): {403},
        ("post", f"{job_path}/capture-sessions"): {409},
        ("get", f"{job_path}/capture-sessions"): set(),
        ("get", f"{job_path}/media-consent-policy"): {503},
        (
            "post",
            f"{job_path}/capture-sessions/{{capture_session_id}}/submit",
        ): {409},
        (
            "get",
            f"{job_path}/capture-sessions/{{capture_session_id}}/analysis",
        ): set(),
        ("get", f"{job_path}/analysis-review"): {403, 409, 503},
        ("post", f"{job_path}/analysis-review/complete"): {403, 409},
        (
            "post",
            f"{job_path}/capture-sessions/{{capture_session_id}}/media-assets/upload",
        ): {409, 503},
        (
            "post",
            f"{job_path}/capture-sessions/{{capture_session_id}}/media-assets/"
            "{media_asset_id}/complete",
        ): {409, 503},
        ("post", f"{job_path}/scope-versions"): {403, 409},
        ("get", f"{job_path}/scope-versions"): {403},
        ("post", f"{job_path}/scope-versions/{{scope_version_id}}/approvals"): {403, 409},
        ("get", f"{job_path}/scope-review"): {403, 409, 503},
        ("get", f"{job_path}/scope-review/history"): {403},
        ("post", f"{job_path}/scope-proposals"): {403, 409},
        ("post", f"{job_path}/scope-review/revision-request"): {403, 409},
        ("post", f"{job_path}/scope-review/confirm"): {403, 409},
        ("post", f"{job_path}/dispatch/setup"): {403, 409},
        ("get", f"{job_path}/dispatch"): {403, 409},
        ("put", f"{job_path}/dispatch"): {403, 409},
        ("get", f"{job_path}/field-brief"): {403, 409},
        ("post", f"{job_path}/check-ins"): {403, 409},
        ("post", f"{job_path}/completion-submissions"): {403, 409},
        ("get", f"{job_path}/completion-summary"): {403, 409, 503},
        ("post", f"{job_path}/completion-requests"): {403, 409},
        (
            "post",
            f"{job_path}/completion-requests/{{request_id}}/revoke",
        ): {403, 409},
        (
            "post",
            f"{job_path}/completion-requests/{{request_id}}/decision",
        ): {403, 409, 503},
        ("get", f"{job_path}/documents/archive"): {403, 409, 503},
        ("post", f"{job_path}/field-issues"): {403, 409},
        ("get", f"{job_path}/field-issues"): {403},
        (
            "get",
            f"{job_path}/field-issues/{{field_issue_id}}/evidence/{{media_asset_id}}/read-url",
        ): {403, 409, 503},
        ("post", f"{job_path}/change-proposals"): {403, 409},
        ("get", f"{job_path}/change-proposals/{{proposal_id}}"): {403, 409, 503},
        ("post", f"{job_path}/change-proposals/{{proposal_id}}/decision"): {
            403,
            409,
        },
        ("post", f"{job_path}/change-proposals/{{proposal_id}}/explanation"): {
            403,
            409,
        },
        ("post", f"{job_path}/change-requests"): {403, 409},
        ("get", f"{job_path}/change-requests"): set(),
        (
            "get",
            f"{job_path}/change-requests/{{change_request_id}}/evidence/"
            "{media_asset_id}/read-url",
        ): {403, 409, 503},
        ("post", f"{job_path}/change-requests/{{change_request_id}}/clarification"): {
            403,
            409,
        },
        ("post", f"{job_path}/change-requests/{{change_request_id}}/explanation"): {
            403,
            409,
        },
        ("post", f"{job_path}/change-requests/{{change_request_id}}/decision"): {403, 409},
        ("post", f"{job_path}/completion-confirmations"): {403, 409, 503},
        ("get", f"{job_path}/completion-confirmations"): set(),
        ("get", f"{job_path}/audit-events"): set(),
        ("get", f"{job_path}/notifications"): set(),
        ("post", f"{job_path}/background-jobs"): {403, 409, 503},
        ("get", f"{job_path}/background-jobs"): set(),
        ("post", f"{job_path}/background-jobs/{{background_job_id}}/retry"): {403, 409},
    }
    documented_statuses = {401, 403, 404, 409, 429, 503}
    common_statuses = {401, 404, 429}
    protected_operations = {
        (method, path): {
            int(code)
            for code in operation["responses"]
            if code.isdigit() and int(code) in documented_statuses
        }
        for path, path_item in schema["paths"].items()
        for method, operation in path_item.items()
        if {"HTTPBearer": []} in operation.get("security", [])
    }

    expected_operations = {
        route: common_statuses | failures for route, failures in route_failures.items()
    }
    expected_operations[("get", "/api/v1/me")] = {401, 429}
    expected_operations[("get", "/api/v1/move-jobs")] = {401, 429}
    expected_operations[("post", "/api/v1/sessions")] = {401, 409, 429}
    assert protected_operations == expected_operations
    assert schema["components"]["schemas"]["HttpExceptionResponse"] == {
        "description": (
            "The string-detail body emitted by the current FastAPI exception handlers."
        ),
        "properties": {"detail": {"title": "Detail", "type": "string"}},
        "required": ["detail"],
        "title": "HttpExceptionResponse",
        "type": "object",
    }
    assert "ErrorResponse" not in schema["components"]["schemas"]
    assert "ErrorDetail" not in schema["components"]["schemas"]
    assert (
        schema["components"]["schemas"]["MediaUploadResponse"]["properties"]["upload_url"]["format"]
        == "uri"
    )

    for method, path in route_failures:
        responses = schema["paths"][path][method]["responses"]
        expected_codes = common_statuses | route_failures[(method, path)]
        if path in {"/api/v1/me", "/api/v1/move-jobs", "/api/v1/sessions"}:
            expected_codes = expected_codes - {404}
        for code in expected_codes:
            assert responses[str(code)]["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/HttpExceptionResponse"
            }
        assert responses["401"]["headers"]["WWW-Authenticate"]["schema"] == {
            "type": "string",
            "example": "Bearer",
        }
        assert responses["429"]["headers"]["Retry-After"]["schema"] == {
            "type": "integer",
            "minimum": 1,
        }


def test_cors_allows_only_the_configured_browser_origin() -> None:
    origin = "https://staging.example.com"
    application = create_app(Settings(environment=AppEnvironment.TEST, frontend_origin=origin))

    with TestClient(application) as client:
        allowed = client.options(
            "/api/v1/move-jobs/00000000-0000-4000-8000-000000000000/dispatch",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "authorization,content-type,traceparent",
            },
        )
        delete_allowed = client.options(
            "/api/v1/move-jobs/00000000-0000-4000-8000-000000000000",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "DELETE",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        rejected = client.options(
            "/api/v1/move-jobs",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == origin
    assert "PUT" in allowed.headers["access-control-allow-methods"]
    assert delete_allowed.status_code == 200
    assert delete_allowed.headers["access-control-allow-origin"] == origin
    assert "DELETE" in delete_allowed.headers["access-control-allow-methods"]
    assert allowed.headers["access-control-allow-credentials"] == "true"
    assert "PATCH" in allowed.headers["access-control-allow-methods"]
    assert "x-seqret-csrf" in allowed.headers["access-control-allow-headers"].lower()
    assert rejected.status_code == 400
    assert "access-control-allow-origin" not in rejected.headers


def test_cors_is_present_on_unhandled_errors() -> None:
    origin = "https://staging.example.com"
    application = create_app(Settings(environment=AppEnvironment.TEST, frontend_origin=origin))

    @application.get("/explode-with-cors")
    async def explode() -> None:
        raise RuntimeError("expected")

    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/explode-with-cors", headers={"Origin": origin})

    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize("environment", [AppEnvironment.STAGING, AppEnvironment.PRODUCTION])
def test_deployed_application_requires_frontend_origin(environment: AppEnvironment) -> None:
    with pytest.raises(ValueError, match="frontend_origin is required"):
        create_app(Settings(environment=environment))

    with pytest.raises(ValueError, match="media storage configuration is required"):
        create_app(
            Settings(
                environment=environment,
                frontend_origin="https://app.example.com",
            )
        )

    with pytest.raises(ValueError, match="media storage configuration is required"):
        create_app(
            Settings(
                environment=environment,
                frontend_origin="https://app.example.com",
                media_bucket_name="seqret-staging-media",
            )
        )


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


def test_production_application_uses_storage_and_disables_documentation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Mock()
    monkeypatch.setattr("app.main.GoogleCloudStorage", Mock(return_value=storage))
    settings = Settings(
        environment=AppEnvironment.PRODUCTION,
        frontend_origin="https://app.example.com",
        media_bucket_name="seqret-production-media",
        storage_signing_service_account_email=(
            "seqret-prd-api@seqret-production.iam.gserviceaccount.com"
        ),
    )
    application = create_app(settings)

    assert application.docs_url is None
    assert application.redoc_url is None
    assert application.openapi_url is None

    with TestClient(application) as client:
        assert application.state.storage_port is storage
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
