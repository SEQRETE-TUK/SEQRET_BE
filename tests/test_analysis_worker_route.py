"""B-06 worker /tasks/analysis route and analysis provider wiring tests."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import AppEnvironment, Settings
from app.contracts.ai import AnalysisRequest, AnalysisTaskV1
from app.contracts.primitives import AnalysisRunId, CaptureSessionId, MediaAssetId, TraceId
from app.entrypoints import worker
from app.modules.analysis.orchestration import AnalysisInputsUnavailableError

TRACE_ID = TraceId("0123456789abcdef0123456789abcdef")
DATABASE_URL = SecretStr("postgresql+psycopg://seqret:secret@localhost/seqret")


def _task() -> AnalysisTaskV1:
    return AnalysisTaskV1(
        analysis_run_id=AnalysisRunId(uuid4()),
        capture_session_id=CaptureSessionId(uuid4()),
        attempt_count=1,
        trace_id=TRACE_ID,
    )


def _request() -> AnalysisRequest:
    return AnalysisRequest(
        analysis_run_id=AnalysisRunId(uuid4()),
        capture_session_id=CaptureSessionId(uuid4()),
        source_media_asset_ids=(MediaAssetId(uuid4()),),
        object_keys=("jobs/1/bedroom.mp4",),
        content_types=("video/mp4",),
        model_name="gemini-2.5-flash",
        model_version="2025-08",
        prompt_version="inventory-1",
    )


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _worker_with_state(*, ai_provider: object | None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: Any) -> AsyncIterator[None]:
        application.state.database_session_factory = _FakeSession
        application.state.storage_port = None
        application.state.ai_provider = ai_provider
        yield

    application = worker.create_worker_app(Settings(environment=AppEnvironment.TEST))
    application.router.lifespan_context = lifespan
    return application


def test_analysis_route_builds_request_and_runs_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    handle = AsyncMock(return_value=None)
    build = AsyncMock(return_value=_request())
    monkeypatch.setattr(worker, "handle_analysis_task", handle)
    monkeypatch.setattr(worker, "build_analysis_request", build)
    application = _worker_with_state(ai_provider=object())

    with TestClient(application) as client:
        response = client.post("/tasks/analysis", json=_task().model_dump(mode="json"))

    assert response.status_code == 204
    build.assert_awaited_once()
    handle.assert_awaited_once()


def test_analysis_route_rejects_invalid_task() -> None:
    application = _worker_with_state(ai_provider=object())

    with TestClient(application) as client:
        assert client.post("/tasks/analysis", json={"schema_version": 1}).status_code == 422


def test_analysis_route_requires_configured_provider() -> None:
    application = _worker_with_state(ai_provider=None)

    with TestClient(application) as client:
        assert (
            client.post("/tasks/analysis", json=_task().model_dump(mode="json")).status_code == 503
        )


def test_analysis_route_acks_when_no_inventory_media(monkeypatch: pytest.MonkeyPatch) -> None:
    handle = AsyncMock(return_value=None)
    monkeypatch.setattr(worker, "handle_analysis_task", handle)
    monkeypatch.setattr(
        worker,
        "build_analysis_request",
        AsyncMock(side_effect=AnalysisInputsUnavailableError("no media")),
    )
    application = _worker_with_state(ai_provider=object())

    with TestClient(application) as client:
        response = client.post("/tasks/analysis", json=_task().model_dump(mode="json"))

    assert response.status_code == 204
    handle.assert_not_awaited()


def test_worker_lifespan_builds_analysis_provider_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "create_database_engine", lambda _: Mock(dispose=AsyncMock()))
    monkeypatch.setattr(worker, "create_session_factory", lambda _: object())
    monkeypatch.setattr(worker, "GoogleCloudStorage", Mock())
    provider = Mock()
    monkeypatch.setattr(worker, "VertexAIProvider", Mock(return_value=provider))
    application = worker.create_worker_app(
        Settings(
            environment=AppEnvironment.TEST,
            database_url=DATABASE_URL,
            media_bucket_name="seqret-media",
            gcp_project_id="seqret-dev",
            analysis_location="us-central1",
        )
    )

    with TestClient(application):
        assert application.state.ai_provider is provider


def test_worker_lifespan_skips_provider_without_analysis_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "create_database_engine", lambda _: Mock(dispose=AsyncMock()))
    monkeypatch.setattr(worker, "create_session_factory", lambda _: object())
    monkeypatch.setattr(worker, "GoogleCloudStorage", Mock())
    application = worker.create_worker_app(
        Settings(
            environment=AppEnvironment.TEST,
            database_url=DATABASE_URL,
            media_bucket_name="seqret-media",
        )
    )

    with TestClient(application):
        assert application.state.ai_provider is None
