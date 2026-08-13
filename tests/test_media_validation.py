"""B-05 media-validation handler tests over StoragePort and A commands."""

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.contracts.actor import ParticipantRole
from app.contracts.fakes import FakeObjectStorage
from app.contracts.maintenance import (
    BackgroundJobType,
    MediaValidationOutcome,
    MediaValidationTaskV1,
)
from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.ports import ProviderError, ProviderErrorKind, StorageObjectMetadata
from app.contracts.primitives import BackgroundJobId, TraceId
from app.modules.background_job.models import BackgroundJob, BackgroundJobStatus
from app.modules.capture.models import CaptureSession, MediaAsset
from app.modules.media_processing.validation import handle_media_validation
from app.modules.move_job.models import (
    JobParticipant,
    Location,
    LocationKind,
    MoveJob,
    MoveJobStatus,
    RoomZone,
)
from app.platform.db import Base, create_session_factory, transactional_session

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
TRACE_ID = TraceId("0123456789abcdef0123456789abcdef")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = (tmp_path / "media-validation.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
    yield create_session_factory(engine)
    await engine.dispose()


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    *,
    status: BackgroundJobStatus = BackgroundJobStatus.QUEUED,
) -> tuple[MediaValidationTaskV1, str]:
    job_id = uuid4()
    participant_id = uuid4()
    location_id = uuid4()
    zone_id = uuid4()
    capture_id = uuid4()
    media_id = uuid4()
    background_job_id = uuid4()
    object_key = f"jobs/{job_id}/capture/{media_id}"
    terminal = status in {BackgroundJobStatus.SUCCEEDED, BackgroundJobStatus.FAILED}
    async with transactional_session(factory) as session:
        session.add(MoveJob(id=job_id, title="검증 테스트", status=MoveJobStatus.DRAFT))
        session.add(
            JobParticipant(
                id=participant_id,
                job_id=job_id,
                role=ParticipantRole.FIELD_WORKER,
                display_name="현장 담당",
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
                media_purpose=MediaPurpose.CONDITION,
                status=(
                    MediaAssetStatus.READY
                    if status is BackgroundJobStatus.SUCCEEDED
                    else MediaAssetStatus.FAILED
                    if status is BackgroundJobStatus.FAILED
                    else MediaAssetStatus.UPLOADED
                ),
                object_key=object_key,
                content_type="image/jpeg",
                expected_size_bytes=14,
                actual_size_bytes=14,
                generation="7",
                sha256_hex=("a" * 64 if status is BackgroundJobStatus.SUCCEEDED else None),
                uploaded_at=NOW,
            )
        )
        session.add(
            BackgroundJob(
                id=background_job_id,
                move_job_id=job_id,
                media_asset_id=media_id,
                job_type=BackgroundJobType.MEDIA_VALIDATION,
                status=status,
                target_object_key=object_key,
                target_generation="7",
                target_content_type="image/jpeg",
                target_size_bytes=14,
                trace_id=TRACE_ID,
                scheduled_at=NOW,
                attempt_count=1,
                last_attempt_at=NOW,
                completed_at=NOW if terminal else None,
                last_error_code=(
                    ProviderErrorKind.UNAVAILABLE.value
                    if status is BackgroundJobStatus.FAILED
                    else None
                ),
            )
        )
    return (
        MediaValidationTaskV1(
            background_job_id=BackgroundJobId(background_job_id),
            attempt_count=1,
            trace_id=TRACE_ID,
        ),
        object_key,
    )


@pytest.mark.anyio
async def test_validation_handler_covers_success_failure_and_terminal_replay(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    content = b"verified-media"
    task, object_key = await _seed(factory)
    storage = FakeObjectStorage()
    storage.metadata[object_key] = StorageObjectMetadata(
        object_key=object_key,
        content_type="image/jpeg",
        size_bytes=len(content),
        generation="7",
    )
    storage.contents[object_key] = content

    result = await handle_media_validation(factory, storage, task, now=NOW)

    assert result is not None
    assert result.outcome is MediaValidationOutcome.SUCCEEDED
    assert result.sha256_hex == hashlib.sha256(content).hexdigest()
    assert await handle_media_validation(factory, storage, task, now=NOW) is None
    async with transactional_session(factory) as session:
        job = await session.get(BackgroundJob, task.background_job_id)
        assert job is not None
        asset = await session.get(MediaAsset, job.media_asset_id)
    assert job.status is BackgroundJobStatus.SUCCEEDED
    assert asset is not None
    assert asset.status is MediaAssetStatus.READY
    assert asset.sha256_hex == result.sha256_hex

    mismatch_task, mismatch_key = await _seed(factory)
    storage.metadata[mismatch_key] = StorageObjectMetadata(
        object_key=mismatch_key,
        content_type="image/png",
        size_bytes=14,
        generation="7",
    )
    mismatch = await handle_media_validation(factory, storage, mismatch_task, now=NOW)
    assert mismatch is not None
    assert mismatch.outcome is MediaValidationOutcome.FAILED
    assert mismatch.error_kind is ProviderErrorKind.INVALID_INPUT

    failing_task, _ = await _seed(factory)
    provider_failure = await handle_media_validation(factory, storage, failing_task, now=NOW)
    assert provider_failure is not None
    assert provider_failure.error_kind is ProviderErrorKind.NOT_FOUND


@pytest.mark.anyio
async def test_validation_handler_rejects_invalid_timeout(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    task, _ = await _seed(factory)
    with pytest.raises(ValueError, match="storage timeouts"):
        await handle_media_validation(
            factory,
            FakeObjectStorage(),
            task,
            now=NOW,
            hash_timeout_seconds=0,
        )


class HashFailureStorage(FakeObjectStorage):
    async def calculate_sha256(
        self,
        *,
        object_key: str,
        generation: str,
        timeout_seconds: float,
    ) -> str:
        del object_key, generation, timeout_seconds
        raise ProviderError(
            ProviderErrorKind.DEADLINE_EXCEEDED,
            "secret provider message",
            retryable=True,
        )


@pytest.mark.anyio
async def test_hash_provider_failure_is_recorded_without_provider_detail(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    task, object_key = await _seed(factory)
    storage = HashFailureStorage()
    storage.metadata[object_key] = StorageObjectMetadata(
        object_key=object_key,
        content_type="image/jpeg",
        size_bytes=14,
        generation="7",
    )

    result = await handle_media_validation(factory, storage, task, now=NOW)

    assert result is not None
    assert result.outcome is MediaValidationOutcome.FAILED
    assert result.error_kind is ProviderErrorKind.DEADLINE_EXCEEDED
    assert "secret provider message" not in repr(result)
