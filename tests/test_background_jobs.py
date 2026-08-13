"""Media-retention target, dispatch, retry, and result tests."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient
from pydantic import JsonValue, ValidationError
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import AppEnvironment, Settings
from app.contracts.fakes import FakeTaskQueue
from app.contracts.maintenance import (
    BackgroundJobType,
    MediaDeletionOutcome,
    MediaDeletionResultV1,
    MediaDeletionTaskV1,
    MediaValidationOutcome,
    MediaValidationResultV1,
    MediaValidationTaskV1,
)
from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.ports import ProviderError, ProviderErrorKind
from app.contracts.primitives import BackgroundJobId, IdempotencyKey, TraceId
from app.main import create_app
from app.modules.background_job.models import BackgroundJob, BackgroundJobStatus
from app.modules.background_job.service import (
    BackgroundJobConflictError,
    BackgroundJobNotFoundError,
    claim_background_jobs,
    complete_media_deletion,
    complete_media_validation,
    create_media_validation_background_job,
    create_retention_background_job,
    dispatch_background_jobs_once,
    finalize_background_job_dispatch,
    retry_background_job,
    start_media_deletion,
    start_media_validation,
)
from app.modules.capture.models import CaptureSession, MediaAsset
from app.modules.move_job.models import MoveJob, MoveJobStatus
from app.platform.db import Base, create_session_factory
from app.platform.event_bus.models import OutboxEvent
from app.runtime import RuntimeKind, create_runtime_context

FIXED_NOW = datetime(2026, 8, 13, 0, 0, tzinfo=UTC)
TRACE_ID = TraceId("0123456789abcdef0123456789abcdef")
BackgroundJobApi = tuple[
    AsyncClient,
    async_sessionmaker[AsyncSession],
    FakeTaskQueue,
    FastAPI,
]


class QueueFailure:
    def __init__(self, error: Exception) -> None:
        self.error = error

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
        del queue_name, handler, payload, idempotency_key, schedule_at, timeout_seconds
        raise self.error


class ExpectedRollback(RuntimeError):
    """Expected exception proving atomic result rollback."""


@pytest.fixture
async def background_job_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[BackgroundJobApi]:
    monkeypatch.setattr("app.modules.background_job.service.utc_now", lambda: FIXED_NOW)
    monkeypatch.setattr("app.modules.background_job.router.utc_now", lambda: FIXED_NOW)
    database_path = (tmp_path / "background-jobs.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
    factory = create_session_factory(engine)
    queue = FakeTaskQueue()
    application = create_app(Settings(environment=AppEnvironment.TEST, media_retention_days=30))
    application.state.database_session_factory = factory
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client, factory, queue, application
    await engine.dispose()


async def _create_job(client: AsyncClient, title: str = "보존 정책 테스트") -> dict[str, Any]:
    response = await client.post(
        "/api/v1/move-jobs",
        json={
            "title": title,
            "participants": [
                {"role": "customer", "display_name": "고객"},
                {"role": "company_manager", "display_name": "관리자"},
                {"role": "field_worker", "display_name": "현장 담당"},
            ],
            "locations": [
                {
                    "kind": "origin",
                    "label": "출발지",
                    "room_zones": [{"name": "거실", "sort_order": 0}],
                }
            ],
        },
    )
    assert response.status_code == 201
    return cast(dict[str, Any], response.json())


def _secret(created: dict[str, Any], role: str) -> str:
    return cast(
        str,
        next(link["secret"] for link in created["access_links"] if link["role"] == role),
    )


def _headers(created: dict[str, Any], role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_secret(created, role)}"}


def _participant_id(created: dict[str, Any], role: str) -> UUID:
    return UUID(
        next(
            participant["id"]
            for participant in created["job"]["participants"]
            if participant["role"] == role
        )
    )


async def _seed_media(
    factory: async_sessionmaker[AsyncSession],
    created: dict[str, Any],
    *,
    status: MediaAssetStatus = MediaAssetStatus.UPLOADED,
    completed_at: datetime | None = FIXED_NOW - timedelta(days=31),
    generation: str | None = "7",
) -> MediaAsset:
    job_id = UUID(created["job"]["id"])
    uploaded = status is not MediaAssetStatus.PENDING_UPLOAD
    asset = MediaAsset(
        capture_session_id=uuid4(),
        room_zone_id=UUID(created["job"]["locations"][0]["room_zones"][0]["id"]),
        media_purpose=MediaPurpose.COMPLETION,
        status=status,
        object_key=f"jobs/{job_id}/retention/{uuid4()}",
        content_type="image/jpeg",
        expected_size_bytes=10,
        actual_size_bytes=10 if uploaded else None,
        generation=generation if uploaded else None,
        uploaded_at=FIXED_NOW - timedelta(days=40) if uploaded else None,
    )
    capture = CaptureSession(
        id=asset.capture_session_id,
        job_id=job_id,
        created_by_participant_id=_participant_id(created, "company_manager"),
    )
    async with factory.begin() as session:
        move_job = await session.get(MoveJob, job_id)
        assert move_job is not None
        if completed_at is not None:
            move_job.status = MoveJobStatus.COMPLETED
            move_job.completed_at = completed_at
        session.add_all((capture, asset))
        await session.flush()
    return asset


def _task(queue: FakeTaskQueue) -> MediaDeletionTaskV1:
    request = next(iter(queue.requests.values()))
    return MediaDeletionTaskV1.model_validate_json(json.dumps(request[2]))


@pytest.mark.anyio
async def test_media_validation_lifecycle_and_retry_boundaries(
    background_job_api: BackgroundJobApi,
) -> None:
    client, factory, queue, _ = background_job_api
    created = await _create_job(client)
    asset = await _seed_media(factory, created)
    async with factory.begin() as session:
        intent = await create_media_validation_background_job(
            session,
            UUID(created["job"]["id"]),
            asset.id,
            _participant_id(created, "company_manager"),
            trace_id=TRACE_ID,
            scheduled_at=FIXED_NOW,
        )
        repeated = await create_media_validation_background_job(
            session,
            UUID(created["job"]["id"]),
            asset.id,
            _participant_id(created, "company_manager"),
            trace_id=TRACE_ID,
            scheduled_at=FIXED_NOW,
        )
        assert repeated.id == intent.id
    missing_asset = await _seed_media(factory, created)
    with pytest.raises(BackgroundJobNotFoundError):
        async with factory.begin() as session:
            await create_media_validation_background_job(
                session,
                uuid4(),
                missing_asset.id,
                _participant_id(created, "company_manager"),
                trace_id=TRACE_ID,
            )
    with pytest.raises(BackgroundJobNotFoundError):
        async with factory.begin() as session:
            await create_media_validation_background_job(
                session,
                UUID(created["job"]["id"]),
                uuid4(),
                _participant_id(created, "company_manager"),
                trace_id=TRACE_ID,
            )
    pending_asset = await _seed_media(
        factory,
        created,
        status=MediaAssetStatus.PENDING_UPLOAD,
        generation=None,
    )
    with pytest.raises(BackgroundJobConflictError):
        async with factory.begin() as session:
            await create_media_validation_background_job(
                session,
                UUID(created["job"]["id"]),
                pending_asset.id,
                _participant_id(created, "company_manager"),
                trace_id=TRACE_ID,
            )

    dispatched = await dispatch_background_jobs_once(
        factory,
        queue,
        queue_name="media-maintenance",
        handler="/tasks/media",
        now=FIXED_NOW,
    )
    assert dispatched == type(dispatched)(claimed=1, queued=1, failed=0)
    request = next(iter(queue.requests.values()))
    task = MediaValidationTaskV1.model_validate_json(json.dumps(request[2]))
    assert task.background_job_id == intent.id
    assert set(request[2]) == {
        "schema_version",
        "background_job_id",
        "job_type",
        "attempt_count",
        "trace_id",
    }

    async with factory.begin() as session:
        work = await start_media_validation(session, task, now=FIXED_NOW)
        assert work is not None
        assert work.source_generation == "7"
        assert work.expected_content_type == "image/jpeg"
        assert work.expected_size_bytes == 10
        assert await start_media_validation(session, task) == work
    with pytest.raises(BackgroundJobConflictError):
        async with factory.begin() as session:
            processing = await session.get(MediaAsset, asset.id)
            assert processing is not None
            processing.status = MediaAssetStatus.FAILED
            await start_media_validation(session, task)
    async with factory() as session:
        processing = await session.get(MediaAsset, asset.id)
        assert processing is not None and processing.status is MediaAssetStatus.PROCESSING

    mismatched = MediaValidationResultV1(
        background_job_id=task.background_job_id,
        attempt_count=task.attempt_count,
        source_generation="7",
        outcome=MediaValidationOutcome.SUCCEEDED,
        observed_content_type="image/png",
        observed_size_bytes=10,
        sha256_hex="a" * 64,
    )
    with pytest.raises(BackgroundJobConflictError):
        async with factory.begin() as session:
            await complete_media_validation(session, mismatched)

    success = mismatched.model_copy(update={"observed_content_type": "image/jpeg"})
    async with factory.begin() as session:
        completed = await complete_media_validation(session, success, completed_at=FIXED_NOW)
        assert completed.status is BackgroundJobStatus.SUCCEEDED
        assert (await complete_media_validation(session, success)) == completed
    async with factory() as session:
        ready = await session.get(MediaAsset, asset.id)
        assert ready is not None
        assert ready.status is MediaAssetStatus.READY
        assert ready.sha256_hex == "a" * 64

    conflict = success.model_copy(update={"sha256_hex": "b" * 64})
    with pytest.raises(BackgroundJobConflictError):
        async with factory.begin() as session:
            await complete_media_validation(session, conflict)
    async with factory.begin() as session:
        assert await start_media_validation(session, task) is None
    invalid_task = task.model_copy(update={"background_job_id": BackgroundJobId(uuid4())})
    with pytest.raises(BackgroundJobNotFoundError):
        async with factory.begin() as session:
            await start_media_validation(session, invalid_task)
    with pytest.raises(ValueError, match="execution_timeout_seconds"):
        async with factory.begin() as session:
            await start_media_validation(session, task, execution_timeout_seconds=0)
    with pytest.raises(BackgroundJobNotFoundError):
        async with factory.begin() as session:
            await complete_media_validation(
                session,
                success.model_copy(update={"background_job_id": BackgroundJobId(uuid4())}),
            )

    failed_asset = await _seed_media(factory, created)
    async with factory.begin() as session:
        failed_intent = await create_media_validation_background_job(
            session,
            UUID(created["job"]["id"]),
            failed_asset.id,
            _participant_id(created, "company_manager"),
            trace_id=TRACE_ID,
            scheduled_at=FIXED_NOW,
        )
    async with factory.begin() as session:
        failed_claim = (await claim_background_jobs(session, now=FIXED_NOW))[0]
        assert isinstance(failed_claim.task, MediaValidationTaskV1)
        await finalize_background_job_dispatch(
            session,
            failed_claim,
            provider_task_id="validation-task",
        )
    with pytest.raises(BackgroundJobNotFoundError):
        async with factory.begin() as session:
            await start_media_validation(
                session,
                failed_claim.task.model_copy(update={"trace_id": TraceId("f" * 32)}),
            )
    with pytest.raises(BackgroundJobConflictError):
        async with factory.begin() as session:
            queued = await session.get(BackgroundJob, failed_intent.id)
            assert queued is not None
            queued.status = BackgroundJobStatus.PENDING
            await start_media_validation(session, failed_claim.task)
    with pytest.raises(BackgroundJobConflictError):
        async with factory.begin() as session:
            queued_asset = await session.get(MediaAsset, failed_asset.id)
            assert queued_asset is not None
            queued_asset.content_type = "image/png"
            await start_media_validation(session, failed_claim.task)
    queued_failure = MediaValidationResultV1(
        background_job_id=BackgroundJobId(failed_intent.id),
        attempt_count=1,
        source_generation="7",
        outcome=MediaValidationOutcome.FAILED,
        error_kind=ProviderErrorKind.UNAVAILABLE,
    )
    with pytest.raises(BackgroundJobConflictError):
        async with factory.begin() as session:
            await complete_media_validation(session, queued_failure)
    async with factory.begin() as session:
        await start_media_validation(session, failed_claim.task, now=FIXED_NOW)
    failure = queued_failure
    async with factory.begin() as session:
        terminal = await complete_media_validation(session, failure, completed_at=FIXED_NOW)
        assert terminal.status is BackgroundJobStatus.FAILED
        assert (await complete_media_validation(session, failure)) == terminal
        retried = await retry_background_job(
            session,
            UUID(created["job"]["id"]),
            failed_intent.id,
            now=FIXED_NOW + timedelta(minutes=1),
        )
        assert retried.status is BackgroundJobStatus.PENDING
    async with factory() as session:
        failed = await session.get(MediaAsset, failed_asset.id)
        assert failed is not None and failed.status is MediaAssetStatus.FAILED

    async with factory.begin() as session:
        retried_claim = (
            await claim_background_jobs(session, now=FIXED_NOW + timedelta(minutes=1))
        )[0]
        assert isinstance(retried_claim.task, MediaValidationTaskV1)
        assert retried_claim.task.attempt_count == 2
        await finalize_background_job_dispatch(
            session,
            retried_claim,
            provider_task_id="validation-retry",
        )
    async with factory.begin() as session:
        await start_media_validation(session, retried_claim.task, now=FIXED_NOW)
    with pytest.raises(BackgroundJobConflictError):
        async with factory.begin() as session:
            await complete_media_validation(session, failure)
    with pytest.raises(BackgroundJobConflictError):
        async with factory.begin() as session:
            running = await session.get(BackgroundJob, failed_intent.id)
            running_asset = await session.get(MediaAsset, failed_asset.id)
            assert running is not None and running_asset is not None
            running.execution_deadline_at = FIXED_NOW
            running_asset.status = MediaAssetStatus.READY
            await retry_background_job(
                session,
                UUID(created["job"]["id"]),
                failed_intent.id,
                now=FIXED_NOW + timedelta(minutes=2),
            )
    async with factory.begin() as session:
        running = await session.get(BackgroundJob, failed_intent.id)
        running_asset = await session.get(MediaAsset, failed_asset.id)
        assert running is not None and running_asset is not None
        running.execution_deadline_at = FIXED_NOW
        retried = await retry_background_job(
            session,
            UUID(created["job"]["id"]),
            failed_intent.id,
            now=FIXED_NOW + timedelta(minutes=2),
        )
        assert retried.status is BackgroundJobStatus.PENDING
        assert running_asset.status is MediaAssetStatus.FAILED


@pytest.mark.anyio
async def test_background_job_api_enforces_policy_and_authorization(
    background_job_api: BackgroundJobApi,
) -> None:
    client, factory, _, application = background_job_api
    created = await _create_job(client)
    asset = await _seed_media(factory, created)
    path = f"/api/v1/move-jobs/{created['job']['id']}/background-jobs"

    assert (await client.post(path, json={"media_asset_id": str(asset.id)})).status_code == 401
    assert (
        await client.post(
            path,
            headers=_headers(created, "customer"),
            json={"media_asset_id": str(asset.id)},
        )
    ).status_code == 403

    created_job = await client.post(
        path,
        headers=_headers(created, "company_manager"),
        json={"media_asset_id": str(asset.id)},
    )
    assert created_job.status_code == 201
    body = created_job.json()
    assert body["status"] == "pending"
    assert body["attempt_count"] == 0
    assert "target_object_key" not in body
    assert "provider_task_id" not in body

    repeated = await client.post(
        path,
        headers=_headers(created, "company_manager"),
        json={"media_asset_id": str(asset.id)},
    )
    assert repeated.status_code == 201
    assert repeated.json()["id"] == body["id"]

    listed = await client.get(path, headers=_headers(created, "field_worker"))
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [body["id"]]

    second = await _create_job(client, "다른 작업")
    cross_job = await client.get(path, headers=_headers(second, "company_manager"))
    assert cross_job.status_code == 404
    missing = await client.post(
        path,
        headers=_headers(created, "company_manager"),
        json={"media_asset_id": str(uuid4())},
    )
    assert missing.status_code == 404

    too_recent = await _seed_media(factory, second, completed_at=FIXED_NOW)
    recent_path = f"/api/v1/move-jobs/{second['job']['id']}/background-jobs"
    recent = await client.post(
        recent_path,
        headers=_headers(second, "company_manager"),
        json={"media_asset_id": str(too_recent.id)},
    )
    assert recent.status_code == 409

    application.state.runtime_context = create_runtime_context(
        RuntimeKind.API,
        Settings(environment=AppEnvironment.TEST),
    )
    unavailable = await client.post(
        path,
        headers=_headers(created, "company_manager"),
        json={"media_asset_id": str(asset.id)},
    )
    assert unavailable.status_code == 503


@pytest.mark.anyio
async def test_dispatch_success_exposes_snapshot_and_commits_one_event(
    background_job_api: BackgroundJobApi,
) -> None:
    client, factory, queue, _ = background_job_api
    created = await _create_job(client)
    asset = await _seed_media(factory, created)
    path = f"/api/v1/move-jobs/{created['job']['id']}/background-jobs"
    response = await client.post(
        path,
        headers=_headers(created, "company_manager"),
        json={"media_asset_id": str(asset.id)},
    )
    background_job_id = UUID(response.json()["id"])

    dispatched = await dispatch_background_jobs_once(
        factory,
        queue,
        queue_name="media-maintenance",
        handler="/tasks/media-delete",
        now=FIXED_NOW,
    )
    assert dispatched.claimed == dispatched.queued == 1
    assert dispatched.failed == 0
    assert (
        await dispatch_background_jobs_once(
            factory,
            queue,
            queue_name="media-maintenance",
            handler="/tasks/media-delete",
            now=FIXED_NOW,
        )
    ).claimed == 0
    task = _task(queue)
    assert task.background_job_id == background_job_id
    assert task.attempt_count == 1
    assert set(next(iter(queue.requests.values()))[2]) == {
        "schema_version",
        "background_job_id",
        "job_type",
        "attempt_count",
        "trace_id",
    }

    async with factory.begin() as session:
        work = await start_media_deletion(session, task, now=FIXED_NOW)
        assert work is not None
        assert work.object_key == asset.object_key
        assert work.generation == "7"
        running = await session.get(BackgroundJob, background_job_id)
        assert running is not None
        assert running.execution_deadline_at == (FIXED_NOW + timedelta(minutes=15)).replace(
            tzinfo=None
        )
        assert await start_media_deletion(session, task) == work

    result = MediaDeletionResultV1(
        background_job_id=BackgroundJobId(background_job_id),
        attempt_count=1,
        outcome=MediaDeletionOutcome.SUCCEEDED,
    )
    async with factory.begin() as session:
        completed = await complete_media_deletion(session, result, completed_at=FIXED_NOW)
        assert completed.status is BackgroundJobStatus.SUCCEEDED
    async with factory.begin() as session:
        assert (await complete_media_deletion(session, result)).status is (
            BackgroundJobStatus.SUCCEEDED
        )
        assert await start_media_deletion(session, task) is None

    async with factory() as session:
        stored_asset = await session.get(MediaAsset, asset.id)
        assert stored_asset is not None
        assert stored_asset.status is MediaAssetStatus.DELETED
        event = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == UUID(created["job"]["id"]))
        )
        assert event is not None
        assert event.payload == {
            "background_job_id": str(background_job_id),
            "media_asset_id": str(asset.id),
        }

    conflict = MediaDeletionResultV1(
        background_job_id=BackgroundJobId(background_job_id),
        attempt_count=1,
        outcome=MediaDeletionOutcome.FAILED,
        error_kind=ProviderErrorKind.PERMISSION_DENIED,
    )
    with pytest.raises(BackgroundJobConflictError):
        async with factory.begin() as session:
            await complete_media_deletion(session, conflict)

    retry_path = f"{path}/{background_job_id}/retry"
    assert (
        await client.post(retry_path, headers=_headers(created, "company_manager"))
    ).status_code == 409


@pytest.mark.anyio
async def test_failed_dispatch_can_be_retried_without_accepting_stale_results(
    background_job_api: BackgroundJobApi,
) -> None:
    client, factory, queue, _ = background_job_api
    created = await _create_job(client)
    asset = await _seed_media(factory, created)
    path = f"/api/v1/move-jobs/{created['job']['id']}/background-jobs"
    created_response = await client.post(
        path,
        headers=_headers(created, "company_manager"),
        json={"media_asset_id": str(asset.id)},
    )
    job_id = UUID(created_response.json()["id"])
    failure = QueueFailure(
        ProviderError(ProviderErrorKind.UNAVAILABLE, "provider detail", retryable=True)
    )
    outcome = await dispatch_background_jobs_once(
        factory,
        failure,
        queue_name="media-maintenance",
        handler="/tasks/media-delete",
        now=FIXED_NOW,
    )
    assert outcome == type(outcome)(claimed=1, queued=0, failed=1)

    retry_path = f"{path}/{job_id}/retry"
    assert (await client.post(retry_path)).status_code == 401
    assert (
        await client.post(retry_path, headers=_headers(created, "field_worker"))
    ).status_code == 403
    retried = await client.post(retry_path, headers=_headers(created, "company_manager"))
    assert retried.status_code == 200
    assert retried.json()["status"] == "pending"
    assert retried.json()["attempt_count"] == 1
    assert (
        await client.post(retry_path, headers=_headers(created, "company_manager"))
    ).status_code == 409

    second = await _create_job(client, "숨김 확인")
    assert (
        await client.post(retry_path, headers=_headers(second, "company_manager"))
    ).status_code == 404
    missing_retry = f"{path}/{uuid4()}/retry"
    assert (
        await client.post(missing_retry, headers=_headers(created, "company_manager"))
    ).status_code == 404

    dispatched = await dispatch_background_jobs_once(
        factory,
        queue,
        queue_name="media-maintenance",
        handler="/tasks/media-delete",
        now=FIXED_NOW + timedelta(minutes=1),
    )
    assert dispatched.queued == 1
    task = _task(queue)
    assert task.attempt_count == 2
    stale = task.model_copy(update={"attempt_count": 1})
    with pytest.raises(BackgroundJobNotFoundError):
        async with factory.begin() as session:
            await start_media_deletion(session, stale)
    async with factory.begin() as session:
        await start_media_deletion(session, task)

    failed_result = MediaDeletionResultV1(
        background_job_id=BackgroundJobId(job_id),
        attempt_count=2,
        outcome=MediaDeletionOutcome.FAILED,
        error_kind=ProviderErrorKind.PERMISSION_DENIED,
    )
    async with factory.begin() as session:
        failed = await complete_media_deletion(session, failed_result, completed_at=FIXED_NOW)
        assert failed.last_error_code == "permission_denied"
    async with factory.begin() as session:
        assert (await complete_media_deletion(session, failed_result)) == failed

    async with factory() as session:
        stored_asset = await session.get(MediaAsset, asset.id)
        assert stored_asset is not None
        assert stored_asset.status is MediaAssetStatus.UPLOADED


@pytest.mark.anyio
async def test_dispatch_leases_validate_inputs_and_recover_after_expiry(
    background_job_api: BackgroundJobApi,
) -> None:
    client, factory, queue, _ = background_job_api
    created = await _create_job(client)
    asset = await _seed_media(factory, created)
    path = f"/api/v1/move-jobs/{created['job']['id']}/background-jobs"
    await client.post(
        path,
        headers=_headers(created, "company_manager"),
        json={"media_asset_id": str(asset.id)},
    )

    async with factory.begin() as session:
        with pytest.raises(ValueError, match="limit"):
            await claim_background_jobs(session, limit=0)
        with pytest.raises(ValueError, match="lease_seconds"):
            await claim_background_jobs(session, lease_seconds=0)
        first = (await claim_background_jobs(session, now=FIXED_NOW, lease_seconds=60))[0]
    async with factory.begin() as session:
        assert (
            await claim_background_jobs(
                session,
                now=FIXED_NOW + timedelta(seconds=59),
            )
            == ()
        )
    async with factory.begin() as session:
        reclaimed = (
            await claim_background_jobs(
                session,
                now=FIXED_NOW + timedelta(seconds=60),
            )
        )[0]
    assert reclaimed.task.attempt_count == first.task.attempt_count == 1
    assert reclaimed.dispatch_token != first.dispatch_token

    async with factory.begin() as session:
        assert not await finalize_background_job_dispatch(
            session,
            first,
            provider_task_id="stale-task",
        )
        with pytest.raises(ValueError, match="exactly one"):
            await finalize_background_job_dispatch(session, reclaimed)
        with pytest.raises(ValueError, match="exactly one"):
            await finalize_background_job_dispatch(
                session,
                reclaimed,
                provider_task_id="task",
                error_code="unexpected",
            )
    async with factory.begin() as session:
        assert isinstance(reclaimed.task, MediaDeletionTaskV1)
        assert await start_media_deletion(session, reclaimed.task, now=FIXED_NOW) is not None
    async with factory.begin() as session:
        assert not await finalize_background_job_dispatch(
            session,
            reclaimed,
            provider_task_id="task",
        )

    job_id = UUID(created["job"]["id"])
    with pytest.raises(BackgroundJobConflictError):
        async with factory.begin() as session:
            await retry_background_job(
                session,
                job_id,
                cast(UUID, reclaimed.task.background_job_id),
                now=FIXED_NOW + timedelta(minutes=14),
            )
    async with factory.begin() as session:
        retried = await retry_background_job(
            session,
            job_id,
            cast(UUID, reclaimed.task.background_job_id),
            now=FIXED_NOW + timedelta(minutes=16),
        )
        assert retried.status is BackgroundJobStatus.PENDING

    with pytest.raises(ValueError, match="execution_timeout_seconds"):
        async with factory.begin() as session:
            assert isinstance(reclaimed.task, MediaDeletionTaskV1)
            await start_media_deletion(session, reclaimed.task, execution_timeout_seconds=0)

    with pytest.raises(ValueError, match="positive"):
        await dispatch_background_jobs_once(
            factory,
            queue,
            queue_name="media-maintenance",
            handler="/tasks/media-delete",
            enqueue_timeout_seconds=0,
        )
    with pytest.raises(ValueError, match="exceed"):
        await dispatch_background_jobs_once(
            factory,
            queue,
            queue_name="media-maintenance",
            handler="/tasks/media-delete",
            lease_seconds=10,
            enqueue_timeout_seconds=10,
        )


@pytest.mark.anyio
async def test_result_boundaries_and_atomic_rollback(background_job_api: BackgroundJobApi) -> None:
    client, factory, queue, _ = background_job_api
    created = await _create_job(client)
    asset = await _seed_media(factory, created)
    path = f"/api/v1/move-jobs/{created['job']['id']}/background-jobs"
    response = await client.post(
        path,
        headers=_headers(created, "company_manager"),
        json={"media_asset_id": str(asset.id)},
    )
    job_id = UUID(response.json()["id"])
    pending_task = MediaDeletionTaskV1(
        background_job_id=BackgroundJobId(job_id),
        attempt_count=1,
        trace_id=TRACE_ID,
    )
    with pytest.raises(BackgroundJobNotFoundError):
        async with factory.begin() as session:
            await start_media_deletion(session, pending_task)
    missing_task = pending_task.model_copy(update={"background_job_id": BackgroundJobId(uuid4())})
    with pytest.raises(BackgroundJobNotFoundError):
        async with factory.begin() as session:
            await start_media_deletion(session, missing_task)

    with pytest.raises(BackgroundJobConflictError):
        async with factory.begin() as session:
            row = await session.get(BackgroundJob, job_id)
            assert row is not None
            row.attempt_count = 1
            row.last_attempt_at = FIXED_NOW
            await session.flush()
            matching_pending_task = pending_task.model_copy(update={"trace_id": row.trace_id})
            await start_media_deletion(session, matching_pending_task)

    await dispatch_background_jobs_once(
        factory,
        queue,
        queue_name="media-maintenance",
        handler="/tasks/media-delete",
        now=FIXED_NOW,
    )
    task = _task(queue)
    bad_trace = task.model_copy(update={"trace_id": TraceId("f" * 32)})
    with pytest.raises(BackgroundJobNotFoundError):
        async with factory.begin() as session:
            await start_media_deletion(session, bad_trace)

    missing_result = MediaDeletionResultV1(
        background_job_id=BackgroundJobId(uuid4()),
        attempt_count=1,
        outcome=MediaDeletionOutcome.SUCCEEDED,
    )
    with pytest.raises(BackgroundJobNotFoundError):
        async with factory.begin() as session:
            await complete_media_deletion(session, missing_result)
    stale_result = missing_result.model_copy(
        update={"background_job_id": BackgroundJobId(job_id), "attempt_count": 2}
    )
    with pytest.raises(BackgroundJobConflictError):
        async with factory.begin() as session:
            await complete_media_deletion(session, stale_result)
    queued_result = MediaDeletionResultV1(
        background_job_id=BackgroundJobId(job_id),
        attempt_count=1,
        outcome=MediaDeletionOutcome.SUCCEEDED,
    )
    with pytest.raises(BackgroundJobConflictError):
        async with factory.begin() as session:
            await complete_media_deletion(session, queued_result)

    async with factory.begin() as session:
        await start_media_deletion(session, task, now=FIXED_NOW)
    success = queued_result
    with pytest.raises(ExpectedRollback):
        async with factory.begin() as session:
            await complete_media_deletion(session, success, completed_at=FIXED_NOW)
            raise ExpectedRollback
    async with factory() as session:
        row = await session.get(BackgroundJob, job_id)
        stored_asset = await session.get(MediaAsset, asset.id)
        assert row is not None and row.status is BackgroundJobStatus.RUNNING
        assert stored_asset is not None and stored_asset.status is MediaAssetStatus.UPLOADED
        assert await session.scalar(select(func.count()).select_from(OutboxEvent)) == 0

    with pytest.raises(ValidationError, match="require one provider error"):
        MediaDeletionResultV1(
            background_job_id=BackgroundJobId(job_id),
            attempt_count=1,
            outcome=MediaDeletionOutcome.FAILED,
        )
    with pytest.raises(ValidationError, match="require one provider error"):
        MediaDeletionResultV1(
            background_job_id=BackgroundJobId(job_id),
            attempt_count=1,
            outcome=MediaDeletionOutcome.SUCCEEDED,
            error_kind=ProviderErrorKind.CONFLICT,
        )


@pytest.mark.anyio
async def test_target_policy_and_unexpected_dispatch_errors(
    background_job_api: BackgroundJobApi,
) -> None:
    client, factory, _, _ = background_job_api
    created = await _create_job(client)
    manager_id = _participant_id(created, "company_manager")
    active_asset = await _seed_media(factory, created, completed_at=None)
    with pytest.raises(ValueError, match="timezone"):
        async with factory.begin() as session:
            await create_retention_background_job(
                session,
                UUID(created["job"]["id"]),
                active_asset.id,
                manager_id,
                retention_cutoff=FIXED_NOW.replace(tzinfo=None),
                trace_id=TRACE_ID,
            )
    with pytest.raises(ValueError, match="scheduled_at"):
        async with factory.begin() as session:
            await create_retention_background_job(
                session,
                UUID(created["job"]["id"]),
                active_asset.id,
                manager_id,
                retention_cutoff=FIXED_NOW,
                trace_id=TRACE_ID,
                scheduled_at=FIXED_NOW.replace(tzinfo=None),
            )
    with pytest.raises(BackgroundJobConflictError):
        async with factory.begin() as session:
            await create_retention_background_job(
                session,
                UUID(created["job"]["id"]),
                active_asset.id,
                manager_id,
                retention_cutoff=FIXED_NOW,
                trace_id=TRACE_ID,
            )

    second = await _create_job(client, "처리 중 미디어")
    processing_asset = await _seed_media(
        factory,
        second,
        status=MediaAssetStatus.PROCESSING,
    )
    with pytest.raises(BackgroundJobConflictError):
        async with factory.begin() as session:
            await create_retention_background_job(
                session,
                UUID(second["job"]["id"]),
                processing_asset.id,
                _participant_id(second, "company_manager"),
                retention_cutoff=FIXED_NOW,
                trace_id=TRACE_ID,
            )

    for invalid_generation in (None, " "):
        generation_missing = await _seed_media(factory, second)
        async with factory.begin() as session:
            await session.execute(text("PRAGMA ignore_check_constraints = ON"))
            stored = await session.get(MediaAsset, generation_missing.id)
            assert stored is not None
            stored.generation = invalid_generation
            await session.flush()
            await session.execute(text("PRAGMA ignore_check_constraints = OFF"))
        with pytest.raises(BackgroundJobConflictError):
            async with factory.begin() as session:
                await create_retention_background_job(
                    session,
                    UUID(second["job"]["id"]),
                    generation_missing.id,
                    _participant_id(second, "company_manager"),
                    retention_cutoff=FIXED_NOW,
                    trace_id=TRACE_ID,
                )

    third = await _create_job(client, "예상치 못한 큐 오류")
    asset = await _seed_media(factory, third)
    path = f"/api/v1/move-jobs/{third['job']['id']}/background-jobs"
    await client.post(
        path,
        headers=_headers(third, "company_manager"),
        json={"media_asset_id": str(asset.id)},
    )
    outcome = await dispatch_background_jobs_once(
        factory,
        QueueFailure(RuntimeError("secret provider detail")),
        queue_name="media-maintenance",
        handler="/tasks/media-delete",
        now=FIXED_NOW,
    )
    assert outcome.failed == 1
    async with factory() as session:
        row = await session.scalar(
            select(BackgroundJob).where(BackgroundJob.media_asset_id == asset.id)
        )
        assert row is not None
        assert row.last_error_code == "unexpected"


@pytest.mark.anyio
@pytest.mark.parametrize("concurrent_exists", [True, False])
async def test_create_recovers_only_a_matching_concurrent_insert(
    concurrent_exists: bool,
) -> None:
    job_id = uuid4()
    media_asset_id = uuid4()
    participant_id = uuid4()
    asset = MediaAsset(
        id=media_asset_id,
        capture_session_id=uuid4(),
        room_zone_id=uuid4(),
        media_purpose=MediaPurpose.COMPLETION,
        status=MediaAssetStatus.UPLOADED,
        object_key=f"jobs/{job_id}/retention/{media_asset_id}",
        content_type="image/jpeg",
        expected_size_bytes=10,
        actual_size_bytes=10,
        generation="7",
        uploaded_at=FIXED_NOW - timedelta(days=40),
    )
    move_job = MoveJob(
        id=job_id,
        title="concurrent creation",
        status=MoveJobStatus.COMPLETED,
        completed_at=FIXED_NOW - timedelta(days=31),
    )
    concurrent = BackgroundJob(
        id=uuid4(),
        move_job_id=job_id,
        media_asset_id=media_asset_id,
        job_type=BackgroundJobType.MEDIA_RETENTION_DELETE,
        status=BackgroundJobStatus.PENDING,
        target_object_key=asset.object_key,
        target_generation="7",
        trace_id=TRACE_ID,
        scheduled_at=FIXED_NOW,
        attempt_count=0,
        created_at=FIXED_NOW,
    )
    session = MagicMock(spec=AsyncSession)
    selected = MagicMock()
    selected.one_or_none.return_value = (asset, move_job)
    session.execute = AsyncMock(return_value=selected)
    session.scalar = AsyncMock(side_effect=[None, concurrent if concurrent_exists else None])
    session.flush = AsyncMock(
        side_effect=IntegrityError("INSERT background_job", {}, RuntimeError("duplicate"))
    )
    nested = MagicMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested.return_value = nested

    if concurrent_exists:
        response = await create_retention_background_job(
            cast(AsyncSession, session),
            job_id,
            media_asset_id,
            participant_id,
            retention_cutoff=FIXED_NOW,
            trace_id=TRACE_ID,
            scheduled_at=FIXED_NOW,
        )
        assert response.id == concurrent.id
    else:
        with pytest.raises(IntegrityError):
            await create_retention_background_job(
                cast(AsyncSession, session),
                job_id,
                media_asset_id,
                participant_id,
                retention_cutoff=FIXED_NOW,
                trace_id=TRACE_ID,
                scheduled_at=FIXED_NOW,
            )
