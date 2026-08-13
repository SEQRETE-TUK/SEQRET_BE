"""Cloud Tasks adapter for authenticated private Cloud Run handlers."""

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Protocol, cast
from urllib.parse import urlsplit

from google.api_core import exceptions as google_exceptions
from google.cloud import tasks_v2
from google.cloud.tasks_v2.types import HttpMethod, HttpRequest, OidcToken, Task
from google.protobuf import duration_pb2, timestamp_pb2  # type: ignore[import-untyped]
from pydantic import JsonValue

from app.contracts.ports import ProviderError, ProviderErrorKind
from app.contracts.primitives import IdempotencyKey

TASK_DISPATCH_DEADLINE_SECONDS = 16 * 60


class CloudTasksClient(Protocol):
    """Cloud Tasks client surface used by the adapter."""

    def queue_path(self, project: str, location: str, queue: str) -> str: ...

    def create_task(
        self,
        request: dict[str, object],
        *,
        timeout: float,
    ) -> Task: ...


def _map_task_error(error: Exception) -> ProviderError:
    if isinstance(error, google_exceptions.NotFound):
        kind, retryable = ProviderErrorKind.NOT_FOUND, False
    elif isinstance(error, (google_exceptions.BadRequest, google_exceptions.InvalidArgument)):
        kind, retryable = ProviderErrorKind.INVALID_INPUT, False
    elif isinstance(error, google_exceptions.Conflict):
        kind, retryable = ProviderErrorKind.CONFLICT, False
    elif isinstance(error, (google_exceptions.Forbidden, google_exceptions.Unauthenticated)):
        kind, retryable = ProviderErrorKind.PERMISSION_DENIED, False
    elif isinstance(error, google_exceptions.DeadlineExceeded):
        kind, retryable = ProviderErrorKind.DEADLINE_EXCEEDED, True
    else:
        kind, retryable = ProviderErrorKind.UNAVAILABLE, True
    return ProviderError(kind, "task enqueue failed", retryable=retryable)


class GoogleCloudTasksQueue:
    """TaskQueuePort adapter using deterministic task IDs and OIDC."""

    def __init__(
        self,
        project_id: str,
        location: str,
        worker_url: str,
        invoker_service_account_email: str,
        *,
        client_factory: Callable[[], CloudTasksClient] | None = None,
    ) -> None:
        parsed = urlsplit(worker_url)
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError("worker_url must be one canonical HTTPS origin") from error
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or worker_url != f"https://{parsed.hostname}"
        ):
            raise ValueError("worker_url must be one canonical HTTPS origin")
        factory = client_factory or (lambda: cast(CloudTasksClient, tasks_v2.CloudTasksClient()))
        self._client = factory()
        self._project_id = project_id
        self._location = location
        self._worker_url = worker_url
        self._invoker_service_account_email = invoker_service_account_email

    @staticmethod
    def _handler_path(handler: str) -> str:
        parsed = urlsplit(handler)
        segments = parsed.path.split("/")[1:]
        if (
            not handler.startswith("/")
            or parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.path != handler
            or any(segment in {"", ".", ".."} for segment in segments)
        ):
            raise ValueError("handler must be one canonical absolute path")
        return parsed.path

    async def enqueue(
        self,
        *,
        queue_name: str,
        handler: str,
        payload: dict[str, JsonValue],
        idempotency_key: IdempotencyKey,
        schedule_at: datetime | None,
        timeout_seconds: float,
    ) -> str:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if schedule_at is not None and schedule_at.utcoffset() is None:
            raise ValueError("schedule_at must include a timezone")

        parent = self._client.queue_path(self._project_id, self._location, queue_name)
        task_name = f"{parent}/tasks/task-{hashlib.sha256(idempotency_key.encode()).hexdigest()}"
        task = Task(
            name=task_name,
            http_request=HttpRequest(
                http_method=HttpMethod.POST,
                url=f"{self._worker_url}{self._handler_path(handler)}",
                headers={"Content-Type": "application/json"},
                body=json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
                oidc_token=OidcToken(
                    service_account_email=self._invoker_service_account_email,
                    audience=self._worker_url,
                ),
            ),
            dispatch_deadline=duration_pb2.Duration(seconds=TASK_DISPATCH_DEADLINE_SECONDS),
        )
        if schedule_at is not None:
            scheduled = timestamp_pb2.Timestamp()
            scheduled.FromDatetime(schedule_at)
            task.schedule_time = scheduled

        try:
            created = await asyncio.wait_for(
                asyncio.to_thread(
                    self._client.create_task,
                    {"parent": parent, "task": task},
                    timeout=timeout_seconds,
                ),
                timeout=timeout_seconds,
            )
        except google_exceptions.AlreadyExists:
            return task_name
        except TimeoutError:
            raise ProviderError(
                ProviderErrorKind.DEADLINE_EXCEEDED,
                "task enqueue failed",
                retryable=True,
            ) from None
        except Exception as error:
            raise _map_task_error(error) from None
        return created.name
