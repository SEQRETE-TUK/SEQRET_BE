"""B-03 analysis run persistence and fake AI draft pipeline tests."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

# Import model modules so create_all sees capture_session/move_job FK targets.
import app.modules.capture.models
import app.modules.move_job.models  # noqa: F401
from app.contracts.ai import AnalysisRequest, AnalysisResult, DraftItem
from app.contracts.fakes import FakeAIProvider
from app.contracts.ports import ProviderErrorKind
from app.contracts.primitives import (
    AnalysisRunId,
    CaptureSessionId,
    IdempotencyKey,
    MediaAssetId,
    TraceId,
)
from app.modules.analysis.models import AiAnalysisRun, AnalysisRunStatus, Detection
from app.modules.analysis.service import (
    AnalysisRunConflictError,
    AnalysisRunNotFoundError,
    complete_analysis_run,
    fail_analysis_run,
    load_analysis_result,
    start_analysis_run,
)
from app.platform.db import Base, create_session_factory, transactional_session

NOW = datetime(2026, 8, 13, 9, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 13, 9, 5, tzinfo=UTC)
TRACE_ID = TraceId("0123456789abcdef0123456789abcdef")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = (tmp_path / "analysis.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
    yield create_session_factory(engine)
    await engine.dispose()


def _request(run_id: AnalysisRunId, capture_id: CaptureSessionId) -> AnalysisRequest:
    return AnalysisRequest(
        analysis_run_id=run_id,
        capture_session_id=capture_id,
        source_media_asset_ids=(MediaAssetId(uuid4()),),
        object_keys=("jobs/1/bedroom.mp4",),
        model_name="fake-vision",
        model_version="2026-08",
        prompt_version="inventory-1",
    )


def _result(
    run_id: AnalysisRunId,
    capture_id: CaptureSessionId,
    *,
    model_name: str = "fake-vision",
    source: MediaAssetId | None = None,
) -> AnalysisResult:
    evidence = (source,) if source is not None else (MediaAssetId(uuid4()),)
    return AnalysisResult(
        analysis_run_id=run_id,
        capture_session_id=capture_id,
        model_name=model_name,
        model_version="2026-08",
        prompt_version="inventory-1",
        draft_items=(
            DraftItem(
                item_key="bed",
                description="퀸 침대",
                confidence=0.9,
                source_media_asset_ids=evidence,
            ),
            DraftItem(item_key="wardrobe", description="옷장", confidence=0.7),
        ),
        review_required_items=(
            DraftItem(item_key="unknown-box", description="확인 필요한 박스", confidence=0.4),
        ),
    )


async def _fake_pipeline(
    factory: async_sessionmaker[AsyncSession],
    request: AnalysisRequest,
) -> AnalysisResult:
    """Drive start -> fake provider -> complete the way a worker later will."""

    async def result_factory(req: AnalysisRequest) -> AnalysisResult:
        return _result(
            req.analysis_run_id, req.capture_session_id, source=req.source_media_asset_ids[0]
        )

    provider = FakeAIProvider(result_factory)
    key = IdempotencyKey(f"analysis:{request.analysis_run_id}")

    async with transactional_session(factory) as session:
        await start_analysis_run(
            session,
            analysis_run_id=request.analysis_run_id,
            capture_session_id=request.capture_session_id,
            trace_id=TRACE_ID,
            now=NOW,
        )
    result = await provider.analyze(request=request, idempotency_key=key, timeout_seconds=30)
    async with transactional_session(factory) as session:
        await complete_analysis_run(session, result=result, now=LATER)
    return result


@pytest.mark.anyio
async def test_fake_pipeline_persists_editable_draft(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = AnalysisRunId(uuid4())
    capture_id = CaptureSessionId(uuid4())
    result = await _fake_pipeline(factory, _request(run_id, capture_id))

    async with transactional_session(factory) as session:
        reloaded = await load_analysis_result(session, analysis_run_id=run_id)
        run = await session.get(AiAnalysisRun, run_id)
        detection_count = await session.scalar(select(func.count()).select_from(Detection))

    assert reloaded == result
    assert run is not None
    assert run.status is AnalysisRunStatus.COMPLETED
    assert run.model_name == "fake-vision"
    assert run.failure_code is None
    assert detection_count == 3


@pytest.mark.anyio
async def test_start_is_idempotent_while_running(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = AnalysisRunId(uuid4())
    capture_id = CaptureSessionId(uuid4())

    for _ in range(2):
        async with transactional_session(factory) as session:
            await start_analysis_run(
                session,
                analysis_run_id=run_id,
                capture_session_id=capture_id,
                trace_id=TRACE_ID,
                now=NOW,
            )

    async with transactional_session(factory) as session:
        run = await session.get(AiAnalysisRun, run_id)
        assert run is not None
        assert run.attempt_count == 1
        assert run.status is AnalysisRunStatus.RUNNING
        assert await load_analysis_result(session, analysis_run_id=run_id) is None


@pytest.mark.anyio
async def test_complete_replay_is_noop(factory: async_sessionmaker[AsyncSession]) -> None:
    run_id = AnalysisRunId(uuid4())
    capture_id = CaptureSessionId(uuid4())
    result = await _fake_pipeline(factory, _request(run_id, capture_id))

    async with transactional_session(factory) as session:
        await complete_analysis_run(session, result=result, now=LATER)

    async with transactional_session(factory) as session:
        detection_count = await session.scalar(select(func.count()).select_from(Detection))
    assert detection_count == 3


@pytest.mark.anyio
async def test_complete_conflict_on_changed_result(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = AnalysisRunId(uuid4())
    capture_id = CaptureSessionId(uuid4())
    await _fake_pipeline(factory, _request(run_id, capture_id))

    async with transactional_session(factory) as session:
        with pytest.raises(AnalysisRunConflictError, match="does not match"):
            await complete_analysis_run(
                session,
                result=_result(run_id, capture_id, model_name="other-model"),
                now=LATER,
            )


@pytest.mark.anyio
async def test_complete_rejects_capture_mismatch(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = AnalysisRunId(uuid4())
    capture_id = CaptureSessionId(uuid4())
    async with transactional_session(factory) as session:
        await start_analysis_run(
            session,
            analysis_run_id=run_id,
            capture_session_id=capture_id,
            trace_id=TRACE_ID,
            now=NOW,
        )

    async with transactional_session(factory) as session:
        with pytest.raises(AnalysisRunConflictError, match="capture session"):
            await complete_analysis_run(
                session,
                result=_result(run_id, CaptureSessionId(uuid4())),
                now=LATER,
            )


@pytest.mark.anyio
async def test_complete_requires_running_run(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = AnalysisRunId(uuid4())
    capture_id = CaptureSessionId(uuid4())
    async with transactional_session(factory) as session:
        await start_analysis_run(
            session,
            analysis_run_id=run_id,
            capture_session_id=capture_id,
            trace_id=TRACE_ID,
            now=NOW,
        )
        await fail_analysis_run(
            session,
            analysis_run_id=run_id,
            error_kind=ProviderErrorKind.UNAVAILABLE,
            now=LATER,
        )

    async with transactional_session(factory) as session:
        with pytest.raises(AnalysisRunConflictError, match="can complete"):
            await complete_analysis_run(session, result=_result(run_id, capture_id), now=LATER)


@pytest.mark.anyio
async def test_complete_missing_run_raises(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = AnalysisRunId(uuid4())
    async with transactional_session(factory) as session:
        with pytest.raises(AnalysisRunNotFoundError):
            await complete_analysis_run(
                session,
                result=_result(run_id, CaptureSessionId(uuid4())),
                now=LATER,
            )


@pytest.mark.anyio
async def test_fail_records_provider_error_and_is_idempotent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = AnalysisRunId(uuid4())
    capture_id = CaptureSessionId(uuid4())
    async with transactional_session(factory) as session:
        await start_analysis_run(
            session,
            analysis_run_id=run_id,
            capture_session_id=capture_id,
            trace_id=TRACE_ID,
            now=NOW,
        )
    for _ in range(2):
        async with transactional_session(factory) as session:
            await fail_analysis_run(
                session,
                analysis_run_id=run_id,
                error_kind=ProviderErrorKind.DEADLINE_EXCEEDED,
                now=LATER,
            )

    async with transactional_session(factory) as session:
        run = await session.get(AiAnalysisRun, run_id)
        assert run is not None
        assert run.status is AnalysisRunStatus.FAILED
        assert run.failure_code == ProviderErrorKind.DEADLINE_EXCEEDED.value


@pytest.mark.anyio
async def test_fail_missing_run_raises(factory: async_sessionmaker[AsyncSession]) -> None:
    async with transactional_session(factory) as session:
        with pytest.raises(AnalysisRunNotFoundError):
            await fail_analysis_run(
                session,
                analysis_run_id=AnalysisRunId(uuid4()),
                error_kind=ProviderErrorKind.UNAVAILABLE,
                now=NOW,
            )


@pytest.mark.anyio
async def test_fail_rejects_completed_run(factory: async_sessionmaker[AsyncSession]) -> None:
    run_id = AnalysisRunId(uuid4())
    capture_id = CaptureSessionId(uuid4())
    await _fake_pipeline(factory, _request(run_id, capture_id))

    async with transactional_session(factory) as session:
        with pytest.raises(AnalysisRunConflictError, match="can fail"):
            await fail_analysis_run(
                session,
                analysis_run_id=run_id,
                error_kind=ProviderErrorKind.UNAVAILABLE,
                now=LATER,
            )


@pytest.mark.anyio
async def test_retry_clears_prior_detections(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = AnalysisRunId(uuid4())
    capture_id = CaptureSessionId(uuid4())
    await _fake_pipeline(factory, _request(run_id, capture_id))

    async with transactional_session(factory) as session:
        await start_analysis_run(
            session,
            analysis_run_id=run_id,
            capture_session_id=capture_id,
            trace_id=TRACE_ID,
            now=LATER,
        )
        run = await session.get(AiAnalysisRun, run_id)
        detection_count = await session.scalar(select(func.count()).select_from(Detection))

    assert run is not None
    assert run.attempt_count == 2
    assert run.status is AnalysisRunStatus.RUNNING
    assert run.model_name is None
    assert detection_count == 0


@pytest.mark.anyio
async def test_load_missing_run_returns_none(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with transactional_session(factory) as session:
        assert await load_analysis_result(session, analysis_run_id=AnalysisRunId(uuid4())) is None
