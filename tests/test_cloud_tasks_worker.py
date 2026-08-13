"""Cloud Tasks adapter and private worker integration tests."""

import hashlib
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from google.api_core import exceptions as google_exceptions
from google.cloud.tasks_v2.types import Task
from pydantic import SecretStr

from app.config import AppEnvironment, Settings
from app.contracts.fakes import FakeObjectStorage
from app.contracts.maintenance import MediaDeletionTaskV1, MediaValidationTaskV1
from app.contracts.ports import ProviderError, ProviderErrorKind
from app.contracts.primitives import BackgroundJobId, IdempotencyKey, TraceId
from app.entrypoints import worker
from app.modules.background_job.service import (
    BackgroundJobConflictError,
    BackgroundJobNotFoundError,
)
from app.platform.task_queue.cloud_tasks import GoogleCloudTasksQueue

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
TRACE_ID = TraceId("0123456789abcdef0123456789abcdef")


class TaskClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.requests: list[tuple[dict[str, object], float]] = []

    def queue_path(self, project: str, location: str, queue: str) -> str:
        return f"projects/{project}/locations/{location}/queues/{queue}"

    def create_task(self, request: dict[str, object], *, timeout: float) -> Task:
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return request["task"]  # type: ignore[return-value]


@pytest.mark.anyio
async def test_cloud_tasks_adapter_builds_one_oidc_task_and_maps_failures() -> None:
    client = TaskClient()
    queue = GoogleCloudTasksQueue(
        "seqret-test",
        "asia-northeast3",
        "https://worker.run.app",
        "seqret-worker@seqret-test.iam.gserviceaccount.com",
        client_factory=lambda: client,
    )
    key = IdempotencyKey("background-job:123:attempt:1")
    task_id = await queue.enqueue(
        queue_name="media",
        handler="/tasks/media",
        payload={"schema_version": 1, "job_type": "media_validation"},
        idempotency_key=key,
        schedule_at=NOW,
        timeout_seconds=5,
    )
    request, timeout = client.requests[0]
    task = request["task"]
    assert isinstance(task, Task)
    expected_name = (
        "projects/seqret-test/locations/asia-northeast3/queues/media/tasks/task-"
        + hashlib.sha256(key.encode()).hexdigest()
    )
    assert task_id == task.name == expected_name
    assert task.http_request.url == "https://worker.run.app/tasks/media"
    assert task.http_request.headers["Content-Type"] == "application/json"
    assert json.loads(task.http_request.body) == {
        "job_type": "media_validation",
        "schema_version": 1,
    }
    assert task.http_request.oidc_token.audience == "https://worker.run.app"
    assert task.http_request.oidc_token.service_account_email.endswith(
        "@seqret-test.iam.gserviceaccount.com"
    )
    assert task.schedule_time == NOW
    assert task.dispatch_deadline.seconds == 960
    assert timeout == 5

    duplicate = TaskClient(google_exceptions.AlreadyExists("duplicate"))  # type: ignore[no-untyped-call]
    duplicate_queue = GoogleCloudTasksQueue(
        "seqret-test",
        "asia-northeast3",
        "https://worker.run.app",
        "seqret-worker@seqret-test.iam.gserviceaccount.com",
        client_factory=lambda: duplicate,
    )
    assert (
        await duplicate_queue.enqueue(
            queue_name="media",
            handler="/tasks/media",
            payload={},
            idempotency_key=key,
            schedule_at=None,
            timeout_seconds=5,
        )
        == expected_name
    )

    for error, kind, retryable in (
        (
            google_exceptions.NotFound("missing"),  # type: ignore[no-untyped-call]
            ProviderErrorKind.NOT_FOUND,
            False,
        ),
        (
            google_exceptions.Conflict("conflict"),  # type: ignore[no-untyped-call]
            ProviderErrorKind.CONFLICT,
            False,
        ),
        (
            google_exceptions.PermissionDenied("denied"),  # type: ignore[no-untyped-call]
            ProviderErrorKind.PERMISSION_DENIED,
            False,
        ),
        (
            google_exceptions.InvalidArgument("bad"),  # type: ignore[no-untyped-call]
            ProviderErrorKind.INVALID_INPUT,
            False,
        ),
        (
            google_exceptions.ServiceUnavailable("down"),  # type: ignore[no-untyped-call]
            ProviderErrorKind.UNAVAILABLE,
            True,
        ),
        (
            google_exceptions.Unauthenticated("unauthenticated"),  # type: ignore[no-untyped-call]
            ProviderErrorKind.PERMISSION_DENIED,
            False,
        ),
        (
            google_exceptions.DeadlineExceeded("deadline"),  # type: ignore[no-untyped-call]
            ProviderErrorKind.DEADLINE_EXCEEDED,
            True,
        ),
    ):
        failing = GoogleCloudTasksQueue(
            "seqret-test",
            "asia-northeast3",
            "https://worker.run.app",
            "seqret-worker@seqret-test.iam.gserviceaccount.com",
            client_factory=lambda error=error: TaskClient(error),  # type: ignore[misc]
        )
        with pytest.raises(ProviderError) as failure:
            await failing.enqueue(
                queue_name="media",
                handler="/tasks/media",
                payload={},
                idempotency_key=key,
                schedule_at=None,
                timeout_seconds=5,
            )
        assert failure.value.kind is kind
        assert failure.value.retryable is retryable
        assert "denied" not in str(failure.value)


@pytest.mark.anyio
async def test_cloud_tasks_adapter_rejects_invalid_boundaries_and_times_out() -> None:
    for url in ("http://worker.run.app", "https://worker.run.app/path", "https:///worker"):
        with pytest.raises(ValueError, match="worker_url"):
            GoogleCloudTasksQueue(
                "seqret-test",
                "asia-northeast3",
                url,
                "seqret-worker@seqret-test.iam.gserviceaccount.com",
                client_factory=TaskClient,
            )
    with pytest.raises(ValueError, match="worker_url"):
        GoogleCloudTasksQueue(
            "seqret-test",
            "asia-northeast3",
            "https://worker.run.app:not-a-port",
            "seqret-worker@seqret-test.iam.gserviceaccount.com",
            client_factory=TaskClient,
        )
    queue = GoogleCloudTasksQueue(
        "seqret-test",
        "asia-northeast3",
        "https://worker.run.app",
        "seqret-worker@seqret-test.iam.gserviceaccount.com",
        client_factory=TaskClient,
    )
    with pytest.raises(ValueError, match="positive"):
        await queue.enqueue(
            queue_name="media",
            handler="/tasks/media",
            payload={},
            idempotency_key=IdempotencyKey("task-key"),
            schedule_at=None,
            timeout_seconds=0,
        )
    for handler in ("tasks/media", "/tasks/../media", "/tasks/media?secret=1"):
        with pytest.raises(ValueError, match="handler"):
            await queue.enqueue(
                queue_name="media",
                handler=handler,
                payload={},
                idempotency_key=IdempotencyKey("task-key"),
                schedule_at=None,
                timeout_seconds=5,
            )
    with pytest.raises(ValueError, match="timezone"):
        await queue.enqueue(
            queue_name="media",
            handler="/tasks/media",
            payload={},
            idempotency_key=IdempotencyKey("task-key"),
            schedule_at=datetime(2026, 8, 13),
            timeout_seconds=5,
        )

    blocking = TaskClient()

    def slow_create(request: dict[str, object], *, timeout: float) -> Task:
        del request, timeout
        time.sleep(0.05)
        return Task()

    blocking.create_task = slow_create  # type: ignore[method-assign]
    timeout_queue = GoogleCloudTasksQueue(
        "seqret-test",
        "asia-northeast3",
        "https://worker.run.app",
        "seqret-worker@seqret-test.iam.gserviceaccount.com",
        client_factory=lambda: blocking,
    )
    with pytest.raises(ProviderError) as failure:
        await timeout_queue.enqueue(
            queue_name="media",
            handler="/tasks/media",
            payload={},
            idempotency_key=IdempotencyKey("task-key"),
            schedule_at=None,
            timeout_seconds=0.001,
        )
    assert failure.value.kind is ProviderErrorKind.DEADLINE_EXCEEDED


def test_private_worker_routes_both_task_contracts_and_discards_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def lifespan(application: Any) -> AsyncIterator[None]:
        application.state.database_session_factory = object()
        application.state.storage_port = FakeObjectStorage()
        yield

    application = worker.create_worker_app(Settings(environment=AppEnvironment.TEST))
    application.router.lifespan_context = lifespan
    validation = AsyncMock(return_value=None)
    deletion = AsyncMock(return_value=None)
    monkeypatch.setattr(worker, "handle_media_validation", validation)
    monkeypatch.setattr(worker, "handle_media_deletion", deletion)
    validation_task = MediaValidationTaskV1(
        background_job_id=BackgroundJobId(UUID("00000000-0000-0000-0000-000000000001")),
        attempt_count=1,
        trace_id=TRACE_ID,
    )
    deletion_task = MediaDeletionTaskV1(
        background_job_id=BackgroundJobId(UUID("00000000-0000-0000-0000-000000000002")),
        attempt_count=1,
        trace_id=TRACE_ID,
    )
    with TestClient(application) as client:
        assert (
            client.post("/tasks/media", json=validation_task.model_dump(mode="json")).status_code
            == 204
        )
        assert (
            client.post("/tasks/media", json=deletion_task.model_dump(mode="json")).status_code
            == 204
        )
        assert client.post("/tasks/media", json={"schema_version": 1}).status_code == 422
        assert client.get("/docs").status_code == 404
        assert client.get("/healthz").json()["runtime"] == "worker"
    validation.assert_awaited_once()
    deletion.assert_awaited_once()

    validation.side_effect = BackgroundJobNotFoundError("stale")
    with TestClient(application) as client:
        assert (
            client.post("/tasks/media", json=validation_task.model_dump(mode="json")).status_code
            == 204
        )

    validation.side_effect = BackgroundJobConflictError("inconsistent")
    with (
        pytest.raises(BackgroundJobConflictError),
        TestClient(application, raise_server_exceptions=True) as client,
    ):
        client.post("/tasks/media", json=validation_task.model_dump(mode="json"))

    validation.side_effect = ProviderError(
        ProviderErrorKind.UNAVAILABLE,
        "retryable provider failure",
        retryable=True,
    )
    with (
        pytest.raises(ProviderError),
        TestClient(application, raise_server_exceptions=True) as client,
    ):
        client.post("/tasks/media", json=validation_task.model_dump(mode="json"))


def test_worker_lifespan_requires_storage_and_disposes_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = Mock(dispose=AsyncMock())
    monkeypatch.setattr(worker, "create_database_engine", lambda _: engine)
    monkeypatch.setattr(worker, "create_session_factory", lambda _: object())
    monkeypatch.setattr(worker, "GoogleCloudStorage", Mock())
    with TestClient(
        worker.create_worker_app(
            Settings(
                environment=AppEnvironment.TEST,
                database_url=SecretStr("postgresql+psycopg://seqret:secret@localhost/seqret"),
                media_bucket_name="seqret-media",
            )
        )
    ) as client:
        assert client.get("/healthz").status_code == 200
    engine.dispose.assert_awaited_once()

    with (
        pytest.raises(ValueError, match="media_bucket_name"),
        TestClient(worker.create_worker_app(Settings(environment=AppEnvironment.TEST))),
    ):
        pass
