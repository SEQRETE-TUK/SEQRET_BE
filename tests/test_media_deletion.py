"""B-07 media-deletion handler tests over the storage port and A commands."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.contracts.actor import ParticipantRole
from app.contracts.fakes import FakeObjectStorage
from app.contracts.maintenance import (
    BackgroundJobType,
    MediaDeletionOutcome,
    MediaDeletionTaskV1,
)
from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.ports import ProviderError, ProviderErrorKind
from app.contracts.primitives import BackgroundJobId, IdempotencyKey, TraceId
from app.modules.background_job.models import BackgroundJob, BackgroundJobStatus
from app.modules.capture.models import CaptureSession, MediaAsset
from app.modules.media_processing.deletion import handle_media_deletion
from app.modules.move_job.models import (
    JobParticipant,
    Location,
    LocationKind,
    MoveJob,
    MoveJobStatus,
    RoomZone,
)
from app.platform.db import Base, create_session_factory, transactional_session
from app.platform.event_bus.models import OutboxEvent

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
TRACE_ID = TraceId("0123456789abcdef0123456789abcdef")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = (tmp_path / "media-deletion.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
    yield create_session_factory(engine)
    await engine.dispose()


class FailingStorage(FakeObjectStorage):
    """Storage stub whose deletion always maps to a retryable provider error."""

    async def delete_object(
        self,
        *,
        object_key: str,
        generation: str | None,
        idempotency_key: IdempotencyKey,
        timeout_seconds: float,
    ) -> None:
        del object_key, generation, idempotency_key, timeout_seconds
        raise ProviderError(ProviderErrorKind.UNAVAILABLE, "storage down", retryable=True)


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    *,
    status: BackgroundJobStatus,
) -> tuple[MediaDeletionTaskV1, str, UUID]:
    job_id = uuid4()
    participant_id = uuid4()
    location_id = uuid4()
    zone_id = uuid4()
    capture_id = uuid4()
    media_id = uuid4()
    bg_id = uuid4()
    object_key = f"jobs/{job_id}/completion/{media_id}"
    terminal = status in {BackgroundJobStatus.SUCCEEDED, BackgroundJobStatus.FAILED}

    async with transactional_session(factory) as session:
        session.add(
            MoveJob(
                id=job_id, title="삭제 테스트", status=MoveJobStatus.COMPLETED, completed_at=NOW
            )
        )
        session.add(
            JobParticipant(
                id=participant_id,
                job_id=job_id,
                role=ParticipantRole.CUSTOMER,
                display_name="고객",
            )
        )
        session.add(
            Location(id=location_id, job_id=job_id, kind=LocationKind.ORIGIN, label="출발지")
        )
        session.add(RoomZone(id=zone_id, location_id=location_id, name="거실", sort_order=0))
        session.add(
            CaptureSession(id=capture_id, job_id=job_id, created_by_participant_id=participant_id)
        )
        session.add(
            MediaAsset(
                id=media_id,
                capture_session_id=capture_id,
                room_zone_id=zone_id,
                media_purpose=MediaPurpose.COMPLETION,
                status=MediaAssetStatus.UPLOADED,
                object_key=object_key,
                content_type="image/jpeg",
                expected_size_bytes=10,
                generation="7",
            )
        )
        session.add(
            BackgroundJob(
                id=bg_id,
                move_job_id=job_id,
                media_asset_id=media_id,
                job_type=BackgroundJobType.MEDIA_RETENTION_DELETE,
                status=status,
                target_object_key=object_key,
                target_generation="7",
                trace_id=TRACE_ID,
                scheduled_at=NOW,
                attempt_count=1,
                last_attempt_at=NOW,
                completed_at=NOW if terminal else None,
            )
        )

    task = MediaDeletionTaskV1(
        background_job_id=BackgroundJobId(bg_id),
        attempt_count=1,
        trace_id=TRACE_ID,
    )
    return task, object_key, media_id


async def _job(
    factory: async_sessionmaker[AsyncSession],
    background_job_id: BackgroundJobId,
) -> BackgroundJob:
    async with transactional_session(factory) as session:
        job = await session.get(BackgroundJob, background_job_id)
    assert job is not None
    return job


@pytest.mark.anyio
async def test_successful_deletion_marks_job_and_asset(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    task, object_key, media_id = await _seed(factory, status=BackgroundJobStatus.QUEUED)
    storage = FakeObjectStorage()

    result = await handle_media_deletion(factory, storage, task, now=NOW)

    assert result is not None
    assert result.outcome is MediaDeletionOutcome.SUCCEEDED
    assert storage.deleted_keys == {object_key}

    job = await _job(factory, task.background_job_id)
    assert job.status is BackgroundJobStatus.SUCCEEDED
    async with transactional_session(factory) as session:
        asset = await session.get(MediaAsset, media_id)
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxEvent))
    assert asset is not None
    assert asset.status is MediaAssetStatus.DELETED
    assert outbox_count == 1


@pytest.mark.anyio
async def test_provider_failure_records_failed_without_deleting(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    task, _object_key, media_id = await _seed(factory, status=BackgroundJobStatus.QUEUED)

    result = await handle_media_deletion(factory, FailingStorage(), task, now=NOW)

    assert result is not None
    assert result.outcome is MediaDeletionOutcome.FAILED
    assert result.error_kind is ProviderErrorKind.UNAVAILABLE

    job = await _job(factory, task.background_job_id)
    assert job.status is BackgroundJobStatus.FAILED
    assert job.last_error_code == ProviderErrorKind.UNAVAILABLE.value
    async with transactional_session(factory) as session:
        asset = await session.get(MediaAsset, media_id)
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxEvent))
    assert asset is not None
    assert asset.status is MediaAssetStatus.UPLOADED
    assert outbox_count == 0


@pytest.mark.anyio
async def test_terminal_job_is_noop(factory: async_sessionmaker[AsyncSession]) -> None:
    task, _object_key, _media_id = await _seed(factory, status=BackgroundJobStatus.SUCCEEDED)
    storage = FakeObjectStorage()

    result = await handle_media_deletion(factory, storage, task, now=NOW)

    assert result is None
    assert storage.deleted_keys == set()
