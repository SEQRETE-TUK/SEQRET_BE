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
from app.contracts.ai import AnalysisRequest, AnalysisResult, AnalysisTaskV1, DraftItem
from app.contracts.ports import ProviderErrorKind
from app.contracts.primitives import AnalysisRunId, CaptureSessionId, MediaAssetId, TraceId
from app.entrypoints import worker
from app.modules.analysis.handler import AnalysisTaskOutcome, AnalysisTaskStatus
from app.modules.analysis.orchestration import AnalysisInputsUnavailableError
from app.modules.analysis.service import AnalysisRetryDecision
from app.modules.analysis_workflow.service import CaptureAnalysisNotFoundError

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

    def begin(self) -> "_FakeSession":
        return self


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


def _result(request: AnalysisRequest) -> AnalysisResult:
    return AnalysisResult(
        analysis_run_id=request.analysis_run_id,
        capture_session_id=request.capture_session_id,
        model_name=request.model_name,
        model_version=request.model_version,
        prompt_version=request.prompt_version,
        draft_items=(
            DraftItem(
                item_key="box",
                description="상자",
                confidence=0.9,
                source_media_asset_ids=request.source_media_asset_ids,
            ),
        ),
    )


def test_analysis_route_builds_request_and_runs_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    analysis_request = _request()
    handle = AsyncMock(return_value=AnalysisTaskOutcome(status=AnalysisTaskStatus.SUCCEEDED))
    build = AsyncMock(return_value=analysis_request)
    start = AsyncMock(return_value=True)
    complete = AsyncMock()
    monkeypatch.setattr(worker, "handle_analysis_task", handle)
    monkeypatch.setattr(worker, "build_analysis_request", build)
    monkeypatch.setattr(worker, "start_capture_analysis", start)
    monkeypatch.setattr(
        worker, "load_analysis_result", AsyncMock(return_value=_result(analysis_request))
    )
    monkeypatch.setattr(worker, "complete_capture_analysis", complete)
    application = _worker_with_state(ai_provider=object())

    with TestClient(application) as client:
        response = client.post("/tasks/analysis", json=_task().model_dump(mode="json"))

    assert response.status_code == 204
    complete.assert_awaited_once()


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
    fail = AsyncMock()
    monkeypatch.setattr(worker, "start_capture_analysis", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "handle_analysis_task", handle)
    monkeypatch.setattr(
        worker,
        "build_analysis_request",
        AsyncMock(side_effect=AnalysisInputsUnavailableError("no media")),
    )
    monkeypatch.setattr(worker, "fail_capture_analysis", fail)
    application = _worker_with_state(ai_provider=object())

    with TestClient(application) as client:
        response = client.post("/tasks/analysis", json=_task().model_dump(mode="json"))

    assert response.status_code == 204
    fail_call = fail.await_args
    assert fail_call is not None
    assert fail_call.kwargs["error_kind"] is ProviderErrorKind.INVALID_INPUT
    assert fail_call.kwargs["retryable"] is False


@pytest.mark.parametrize("terminal", [False, True])
def test_analysis_route_acks_missing_or_terminal_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    terminal: bool,
) -> None:
    start = AsyncMock(
        return_value=False,
        side_effect=None if terminal else CaptureAnalysisNotFoundError("stale"),
    )
    build = AsyncMock()
    monkeypatch.setattr(worker, "start_capture_analysis", start)
    monkeypatch.setattr(worker, "build_analysis_request", build)
    application = _worker_with_state(ai_provider=object())

    with TestClient(application) as client:
        response = client.post("/tasks/analysis", json=_task().model_dump(mode="json"))

    assert response.status_code == 204


def _retryable_failure_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    decision: AnalysisRetryDecision,
    prepare: AsyncMock,
    fail: AsyncMock,
) -> FastAPI:
    monkeypatch.setattr(worker, "start_capture_analysis", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "build_analysis_request", AsyncMock(return_value=_request()))
    monkeypatch.setattr(
        worker,
        "handle_analysis_task",
        AsyncMock(
            return_value=AnalysisTaskOutcome(
                status=AnalysisTaskStatus.FAILED,
                error_kind=ProviderErrorKind.UNAVAILABLE,
                retryable=True,
            )
        ),
    )
    prepare.return_value = decision
    monkeypatch.setattr(worker, "prepare_analysis_retry", prepare)
    monkeypatch.setattr(worker, "fail_capture_analysis", fail)
    return _worker_with_state(ai_provider=object())


def test_analysis_route_retries_retryable_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    prepare = AsyncMock()
    fail = AsyncMock()
    application = _retryable_failure_app(
        monkeypatch, decision=AnalysisRetryDecision.RETRY, prepare=prepare, fail=fail
    )

    with TestClient(application) as client:
        response = client.post("/tasks/analysis", json=_task().model_dump(mode="json"))

    assert response.status_code == 503


def test_analysis_route_terminates_when_retries_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    prepare = AsyncMock()
    fail = AsyncMock()
    application = _retryable_failure_app(
        monkeypatch, decision=AnalysisRetryDecision.TERMINAL, prepare=prepare, fail=fail
    )

    with TestClient(application) as client:
        response = client.post("/tasks/analysis", json=_task().model_dump(mode="json"))

    assert response.status_code == 204
    fail_call = fail.await_args
    assert fail_call is not None
    assert fail_call.kwargs["error_kind"] is ProviderErrorKind.UNAVAILABLE
    assert fail_call.kwargs["retryable"] is True


def test_analysis_route_fails_when_completed_result_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fail = AsyncMock()
    monkeypatch.setattr(worker, "start_capture_analysis", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "build_analysis_request", AsyncMock(return_value=_request()))
    monkeypatch.setattr(
        worker,
        "handle_analysis_task",
        AsyncMock(return_value=AnalysisTaskOutcome(status=AnalysisTaskStatus.SUCCEEDED)),
    )
    monkeypatch.setattr(worker, "load_analysis_result", AsyncMock(return_value=None))
    monkeypatch.setattr(worker, "fail_capture_analysis", fail)
    application = _worker_with_state(ai_provider=object())

    with TestClient(application) as client:
        response = client.post("/tasks/analysis", json=_task().model_dump(mode="json"))

    assert response.status_code == 204
    fail_call = fail.await_args
    assert fail_call is not None
    assert fail_call.kwargs["error_kind"] is ProviderErrorKind.CONFLICT
    assert fail_call.kwargs["retryable"] is False


def test_analysis_route_normalizes_stored_failure_without_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fail = AsyncMock()
    monkeypatch.setattr(worker, "start_capture_analysis", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "build_analysis_request", AsyncMock(return_value=_request()))
    monkeypatch.setattr(
        worker,
        "handle_analysis_task",
        AsyncMock(return_value=AnalysisTaskOutcome(status=AnalysisTaskStatus.FAILED)),
    )
    monkeypatch.setattr(worker, "fail_capture_analysis", fail)
    application = _worker_with_state(ai_provider=object())

    with TestClient(application) as client:
        response = client.post("/tasks/analysis", json=_task().model_dump(mode="json"))

    assert response.status_code == 204
    fail_call = fail.await_args
    assert fail_call is not None
    assert fail_call.kwargs["error_kind"] is ProviderErrorKind.CONFLICT


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
