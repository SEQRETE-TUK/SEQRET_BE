"""B-06 analysis enqueue and work-lookup orchestration tests."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.modules.move_job.models  # noqa: F401
from app.contracts.ai import AnalysisTaskV1
from app.contracts.fakes import FakeTaskQueue
from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.primitives import AnalysisRunId, CaptureSessionId, TraceId
from app.modules.analysis.orchestration import (
    AnalysisInputsUnavailableError,
    build_analysis_request,
    request_analysis,
)
from app.modules.capture.models import MediaAsset
from app.platform.db import Base, create_session_factory, transactional_session

NOW = datetime(2026, 8, 13, 11, 0, tzinfo=UTC)
TRACE_ID = TraceId("0123456789abcdef0123456789abcdef")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = (tmp_path / "analysis-orchestration.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
    yield create_session_factory(engine)
    await engine.dispose()


def _asset(
    capture_id: UUID,
    *,
    purpose: MediaPurpose,
    status: MediaAssetStatus,
    created_at: datetime,
) -> MediaAsset:
    return MediaAsset(
        capture_session_id=capture_id,
        room_zone_id=uuid4(),
        media_purpose=purpose,
        status=status,
        # Production object keys are opaque UUID paths and do not carry a suffix.
        object_key=f"jobs/1/{uuid4()}",
        content_type="image/jpeg",
        expected_size_bytes=10,
        actual_size_bytes=10,
        sha256_hex="a" * 64,
        generation="7",
        created_at=created_at,
        uploaded_at=created_at,
    )


@pytest.mark.anyio
async def test_build_request_uses_ordered_ready_inventory_media(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    capture_id = uuid4()
    first = _asset(
        capture_id, purpose=MediaPurpose.INVENTORY, status=MediaAssetStatus.READY, created_at=NOW
    )
    second = _asset(
        capture_id,
        purpose=MediaPurpose.INVENTORY,
        status=MediaAssetStatus.READY,
        created_at=NOW + timedelta(seconds=1),
    )
    async with transactional_session(factory) as session:
        session.add_all([second, first])

    run_id = AnalysisRunId(uuid4())
    async with transactional_session(factory) as session:
        request = await build_analysis_request(
            session,
            analysis_run_id=run_id,
            capture_session_id=CaptureSessionId(capture_id),
            model_name="gemini-2.5-flash",
            model_version="2025-08",
            prompt_version="inventory-1",
        )

    assert request.analysis_run_id == run_id
    assert request.object_keys == (first.object_key, second.object_key)
    assert request.source_media_asset_ids == (first.id, second.id)
    assert request.content_types == (first.content_type, second.content_type)


@pytest.mark.anyio
async def test_build_request_ignores_non_ready_or_non_inventory(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    capture_id = uuid4()
    ready_inventory = _asset(
        capture_id, purpose=MediaPurpose.INVENTORY, status=MediaAssetStatus.READY, created_at=NOW
    )
    pending_inventory = _asset(
        capture_id,
        purpose=MediaPurpose.INVENTORY,
        status=MediaAssetStatus.UPLOADED,
        created_at=NOW,
    )
    ready_condition = _asset(
        capture_id, purpose=MediaPurpose.CONDITION, status=MediaAssetStatus.READY, created_at=NOW
    )
    async with transactional_session(factory) as session:
        session.add_all([ready_inventory, pending_inventory, ready_condition])

    async with transactional_session(factory) as session:
        request = await build_analysis_request(
            session,
            analysis_run_id=AnalysisRunId(uuid4()),
            capture_session_id=CaptureSessionId(capture_id),
            model_name="gemini-2.5-flash",
            model_version="2025-08",
            prompt_version="inventory-1",
        )

    assert request.object_keys == (ready_inventory.object_key,)
    assert request.content_types == (ready_inventory.content_type,)


@pytest.mark.anyio
async def test_build_request_without_media_raises(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with transactional_session(factory) as session:
        with pytest.raises(AnalysisInputsUnavailableError):
            await build_analysis_request(
                session,
                analysis_run_id=AnalysisRunId(uuid4()),
                capture_session_id=CaptureSessionId(uuid4()),
                model_name="gemini-2.5-flash",
                model_version="2025-08",
                prompt_version="inventory-1",
            )


@pytest.mark.anyio
async def test_request_analysis_enqueues_minimal_task_idempotently() -> None:
    queue = FakeTaskQueue()
    run_id = AnalysisRunId(uuid4())
    capture_id = CaptureSessionId(uuid4())

    async def enqueue_once() -> str:
        return await request_analysis(
            queue,
            analysis_run_id=run_id,
            capture_session_id=capture_id,
            trace_id=TRACE_ID,
            queue_name="analysis",
            handler="/tasks/analysis",
        )

    first = await enqueue_once()
    second = await enqueue_once()

    assert first == second
    assert len(queue.enqueued) == 1
    _, handler, payload, _ = next(iter(queue.requests.values()))
    assert handler == "/tasks/analysis"
    assert payload["analysis_run_id"] == str(run_id)
    assert payload["capture_session_id"] == str(capture_id)
    assert payload["attempt_count"] == 1


def test_analysis_task_rejects_nonpositive_attempt() -> None:
    with pytest.raises(ValidationError):
        AnalysisTaskV1(
            analysis_run_id=AnalysisRunId(uuid4()),
            capture_session_id=CaptureSessionId(uuid4()),
            attempt_count=0,
            trace_id=TRACE_ID,
        )
