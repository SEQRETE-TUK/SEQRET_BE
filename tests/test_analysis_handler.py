"""B-06 analysis worker handler idempotency and provider-error tests."""

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
from app.contracts.ports import ProviderError, ProviderErrorKind
from app.contracts.primitives import (
    AnalysisRunId,
    CaptureSessionId,
    IdempotencyKey,
    MediaAssetId,
    TraceId,
)
from app.modules.analysis.handler import (
    AnalysisTaskStatus,
    handle_analysis_task,
)
from app.modules.analysis.models import AiAnalysisRun, AnalysisRunStatus, Detection
from app.modules.analysis.service import (
    AnalysisRetryDecision,
    AnalysisRunConflictError,
    AnalysisRunNotFoundError,
    fail_analysis_run,
    get_analysis_run_snapshot,
    prepare_analysis_retry,
    reopen_analysis_run,
    start_analysis_run,
)
from app.platform.db import Base, create_session_factory, transactional_session

NOW = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)
TRACE_ID = TraceId("0123456789abcdef0123456789abcdef")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = (tmp_path / "analysis-handler.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
    yield create_session_factory(engine)
    await engine.dispose()


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


def _result(request: AnalysisRequest) -> AnalysisResult:
    return AnalysisResult(
        analysis_run_id=request.analysis_run_id,
        capture_session_id=request.capture_session_id,
        model_name=request.model_name,
        model_version=request.model_version,
        prompt_version=request.prompt_version,
        draft_items=(
            DraftItem(
                item_key="bed",
                description="퀸 침대",
                confidence=0.9,
                source_media_asset_ids=(request.source_media_asset_ids[0],),
            ),
        ),
    )


class CountingProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def analyze(
        self,
        *,
        request: AnalysisRequest,
        idempotency_key: IdempotencyKey,
        timeout_seconds: float,
    ) -> AnalysisResult:
        del idempotency_key, timeout_seconds
        self.calls += 1
        return _result(request)


class FailingProvider:
    def __init__(self, *, kind: ProviderErrorKind, retryable: bool) -> None:
        self._kind = kind
        self._retryable = retryable
        self.calls = 0

    async def analyze(
        self,
        *,
        request: AnalysisRequest,
        idempotency_key: IdempotencyKey,
        timeout_seconds: float,
    ) -> AnalysisResult:
        del request, idempotency_key, timeout_seconds
        self.calls += 1
        raise ProviderError(self._kind, "provider unavailable", retryable=self._retryable)


@pytest.mark.anyio
async def test_fresh_success_persists_completed_draft(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _request()
    provider = CountingProvider()

    outcome = await handle_analysis_task(factory, provider, request, trace_id=TRACE_ID, now=NOW)

    assert outcome.status is AnalysisTaskStatus.SUCCEEDED
    assert provider.calls == 1
    async with transactional_session(factory) as session:
        run = await session.get(AiAnalysisRun, request.analysis_run_id)
        detections = await session.scalar(select(func.count()).select_from(Detection))
    assert run is not None
    assert run.status is AnalysisRunStatus.COMPLETED
    assert detections == 1


@pytest.mark.anyio
async def test_provider_failure_records_failed_outcome(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _request()
    provider = FailingProvider(kind=ProviderErrorKind.UNAVAILABLE, retryable=True)

    outcome = await handle_analysis_task(factory, provider, request, trace_id=TRACE_ID, now=NOW)

    assert outcome.status is AnalysisTaskStatus.FAILED
    assert outcome.error_kind is ProviderErrorKind.UNAVAILABLE
    assert outcome.retryable is True
    async with transactional_session(factory) as session:
        run = await session.get(AiAnalysisRun, request.analysis_run_id)
    assert run is not None
    assert run.status is AnalysisRunStatus.FAILED


@pytest.mark.anyio
async def test_duplicate_delivery_after_success_skips_provider(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _request()
    provider = CountingProvider()

    first = await handle_analysis_task(factory, provider, request, trace_id=TRACE_ID, now=NOW)
    second = await handle_analysis_task(factory, provider, request, trace_id=TRACE_ID, now=NOW)

    assert first.status is AnalysisTaskStatus.SUCCEEDED
    assert second.status is AnalysisTaskStatus.SUCCEEDED
    assert provider.calls == 1


@pytest.mark.anyio
async def test_duplicate_delivery_after_failure_stays_terminal(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _request()
    failing = FailingProvider(kind=ProviderErrorKind.PERMISSION_DENIED, retryable=False)
    await handle_analysis_task(factory, failing, request, trace_id=TRACE_ID, now=NOW)

    retry_provider = CountingProvider()
    outcome = await handle_analysis_task(
        factory, retry_provider, request, trace_id=TRACE_ID, now=NOW
    )

    assert outcome.status is AnalysisTaskStatus.FAILED
    assert outcome.retryable is False
    assert retry_provider.calls == 0


@pytest.mark.parametrize(
    ("kind", "retryable"),
    [
        (ProviderErrorKind.UNAVAILABLE, True),
        (ProviderErrorKind.DEADLINE_EXCEEDED, True),
        (ProviderErrorKind.PERMISSION_DENIED, False),
    ],
)
@pytest.mark.anyio
async def test_redelivery_of_stored_failure_restores_retryability(
    factory: async_sessionmaker[AsyncSession],
    kind: ProviderErrorKind,
    retryable: bool,
) -> None:
    # Simulate a crash between the FAILED commit and the reopen/503 decision:
    # the run is already FAILED when the same task is redelivered.
    request = _request()
    async with transactional_session(factory) as session:
        await start_analysis_run(
            session,
            analysis_run_id=request.analysis_run_id,
            capture_session_id=request.capture_session_id,
            trace_id=TRACE_ID,
            now=NOW,
        )
        await fail_analysis_run(
            session,
            analysis_run_id=request.analysis_run_id,
            error_kind=kind,
            now=NOW,
        )

    provider = CountingProvider()
    outcome = await handle_analysis_task(factory, provider, request, trace_id=TRACE_ID, now=NOW)

    assert outcome.status is AnalysisTaskStatus.FAILED
    assert outcome.error_kind is kind
    assert outcome.retryable is retryable
    assert provider.calls == 0


@pytest.mark.anyio
async def test_snapshot_reflects_running_run_without_failure(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _request()
    async with transactional_session(factory) as session:
        await start_analysis_run(
            session,
            analysis_run_id=request.analysis_run_id,
            capture_session_id=request.capture_session_id,
            trace_id=TRACE_ID,
            now=NOW,
        )

    async with transactional_session(factory) as session:
        snapshot = await get_analysis_run_snapshot(session, analysis_run_id=request.analysis_run_id)
    assert snapshot.status is AnalysisRunStatus.RUNNING
    assert snapshot.failure_kind is None


@pytest.mark.anyio
async def test_redelivery_of_running_run_completes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _request()
    async with transactional_session(factory) as session:
        await start_analysis_run(
            session,
            analysis_run_id=request.analysis_run_id,
            capture_session_id=request.capture_session_id,
            trace_id=TRACE_ID,
            now=NOW,
        )
    provider = CountingProvider()

    outcome = await handle_analysis_task(factory, provider, request, trace_id=TRACE_ID, now=NOW)

    assert outcome.status is AnalysisTaskStatus.SUCCEEDED
    assert provider.calls == 1


@pytest.mark.anyio
async def test_snapshot_raises_for_missing_run(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with transactional_session(factory) as session:
        with pytest.raises(AnalysisRunNotFoundError):
            await get_analysis_run_snapshot(session, analysis_run_id=AnalysisRunId(uuid4()))


@pytest.mark.anyio
async def test_snapshot_reflects_failed_run_failure_kind(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _request()
    await _fail_fresh_run(factory, request)

    async with transactional_session(factory) as session:
        snapshot = await get_analysis_run_snapshot(session, analysis_run_id=request.analysis_run_id)
    assert snapshot.status is AnalysisRunStatus.FAILED
    assert snapshot.failure_kind is ProviderErrorKind.UNAVAILABLE


async def _fail_fresh_run(
    factory: async_sessionmaker[AsyncSession],
    request: AnalysisRequest,
) -> None:
    async with transactional_session(factory) as session:
        await start_analysis_run(
            session,
            analysis_run_id=request.analysis_run_id,
            capture_session_id=request.capture_session_id,
            trace_id=TRACE_ID,
            now=NOW,
        )
        await fail_analysis_run(
            session,
            analysis_run_id=request.analysis_run_id,
            error_kind=ProviderErrorKind.UNAVAILABLE,
            now=NOW,
        )


@pytest.mark.anyio
async def test_reopen_failed_run_starts_new_attempt(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _request()
    await _fail_fresh_run(factory, request)

    async with transactional_session(factory) as session:
        await reopen_analysis_run(session, analysis_run_id=request.analysis_run_id, now=NOW)

    async with transactional_session(factory) as session:
        run = await session.get(AiAnalysisRun, request.analysis_run_id)
    assert run is not None
    assert run.status is AnalysisRunStatus.RUNNING
    assert run.attempt_count == 2
    assert run.failure_code is None


@pytest.mark.anyio
async def test_reopen_running_run_is_noop(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _request()
    async with transactional_session(factory) as session:
        await start_analysis_run(
            session,
            analysis_run_id=request.analysis_run_id,
            capture_session_id=request.capture_session_id,
            trace_id=TRACE_ID,
            now=NOW,
        )
        await reopen_analysis_run(session, analysis_run_id=request.analysis_run_id, now=NOW)
        run = await session.get(AiAnalysisRun, request.analysis_run_id)
    assert run is not None
    assert run.status is AnalysisRunStatus.RUNNING
    assert run.attempt_count == 1


@pytest.mark.anyio
async def test_reopen_completed_run_conflicts(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _request()
    await handle_analysis_task(factory, CountingProvider(), request, trace_id=TRACE_ID, now=NOW)

    async with transactional_session(factory) as session:
        with pytest.raises(AnalysisRunConflictError, match="can be retried"):
            await reopen_analysis_run(session, analysis_run_id=request.analysis_run_id, now=NOW)


@pytest.mark.anyio
async def test_reopen_missing_run_raises(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with transactional_session(factory) as session:
        with pytest.raises(AnalysisRunNotFoundError):
            await reopen_analysis_run(session, analysis_run_id=AnalysisRunId(uuid4()), now=NOW)


@pytest.mark.anyio
async def test_prepare_retry_reopens_failed_run_below_limit(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _request()
    await _fail_fresh_run(factory, request)

    async with transactional_session(factory) as session:
        decision = await prepare_analysis_retry(
            session,
            analysis_run_id=request.analysis_run_id,
            max_attempts=5,
            now=NOW,
        )

    assert decision is AnalysisRetryDecision.RETRY
    async with transactional_session(factory) as session:
        run = await session.get(AiAnalysisRun, request.analysis_run_id)
    assert run is not None
    assert run.status is AnalysisRunStatus.RUNNING
    assert run.attempt_count == 2
    assert run.failure_code is None


@pytest.mark.anyio
async def test_prepare_retry_terminal_when_attempts_exhausted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    request = _request()
    await _fail_fresh_run(factory, request)

    async with transactional_session(factory) as session:
        decision = await prepare_analysis_retry(
            session,
            analysis_run_id=request.analysis_run_id,
            max_attempts=1,
            now=NOW,
        )

    assert decision is AnalysisRetryDecision.TERMINAL
    async with transactional_session(factory) as session:
        run = await session.get(AiAnalysisRun, request.analysis_run_id)
    assert run is not None
    assert run.status is AnalysisRunStatus.FAILED
    assert run.attempt_count == 1
    assert run.failure_code == ProviderErrorKind.UNAVAILABLE.value


@pytest.mark.anyio
async def test_prepare_retry_terminal_when_run_absent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with transactional_session(factory) as session:
        decision = await prepare_analysis_retry(
            session,
            analysis_run_id=AnalysisRunId(uuid4()),
            max_attempts=5,
            now=NOW,
        )
    assert decision is AnalysisRetryDecision.TERMINAL


@pytest.mark.anyio
async def test_prepare_retry_on_already_reopened_run_defers_to_retry(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # Models the concurrent-redelivery loser: the first delivery already reopened
    # the run to RUNNING, so the second must return RETRY (defer to the 503) and
    # never finalize the dispatch, leaving the attempt count untouched.
    request = _request()
    await _fail_fresh_run(factory, request)
    async with transactional_session(factory) as session:
        await reopen_analysis_run(session, analysis_run_id=request.analysis_run_id, now=NOW)

    async with transactional_session(factory) as session:
        decision = await prepare_analysis_retry(
            session,
            analysis_run_id=request.analysis_run_id,
            max_attempts=5,
            now=NOW,
        )

    assert decision is AnalysisRetryDecision.RETRY
    async with transactional_session(factory) as session:
        run = await session.get(AiAnalysisRun, request.analysis_run_id)
    assert run is not None
    assert run.status is AnalysisRunStatus.RUNNING
    assert run.attempt_count == 2
