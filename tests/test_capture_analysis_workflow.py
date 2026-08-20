"""INT-01 capture submission, dispatch, and scope-import workflow tests."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue
from sqlalchemy import create_engine, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.contracts.actor import ParticipantRole
from app.contracts.ai import (
    AnalysisFailureDetail,
    AnalysisFailureStage,
    AnalysisResult,
    AnalysisTaskV1,
    DraftItem,
)
from app.contracts.fakes import FakeTaskQueue
from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.ports import ProviderError, ProviderErrorKind
from app.contracts.primitives import (
    AnalysisRunId,
    CaptureSessionId,
    IdempotencyKey,
    MediaAssetId,
    TraceId,
)
from app.modules.analysis_workflow.models import (
    CaptureAnalysisDispatch,
    CaptureAnalysisStatus,
)
from app.modules.analysis_workflow.service import (
    AnalysisDispatchResult,
    CaptureAnalysisConflictError,
    CaptureAnalysisNotFoundError,
    ClaimedCaptureAnalysis,
    claim_capture_analyses,
    complete_capture_analysis,
    dispatch_capture_analyses_once,
    fail_capture_analysis,
    finalize_capture_analysis_dispatch,
    get_capture_analysis,
    start_capture_analysis,
    submit_capture_analysis,
)
from app.modules.capture.models import CaptureSession, MediaAsset
from app.modules.move_job.models import (
    JobParticipant,
    Location,
    LocationKind,
    MoveJob,
    MoveJobStatus,
    RoomZone,
)
from app.modules.scope.models import ScopeVersion
from app.platform.db import Base, create_session_factory
from app.platform.event_bus.models import OutboxEvent

NOW = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
TRACE_ID = TraceId("0123456789abcdef0123456789abcdef")


@dataclass(frozen=True)
class CaptureSeed:
    job_id: UUID
    participant_id: UUID
    capture_id: UUID
    room_zone_id: UUID
    asset_ids: tuple[UUID, ...]


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


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = (tmp_path / "capture-analysis.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
    yield create_session_factory(engine)
    await engine.dispose()


async def _seed_capture(
    factory: async_sessionmaker[AsyncSession],
    *statuses: MediaAssetStatus,
    purposes: tuple[MediaPurpose, ...] | None = None,
) -> CaptureSeed:
    job_id = uuid4()
    participant_id = uuid4()
    capture_id = uuid4()
    location_id = uuid4()
    room_zone_id = uuid4()
    purposes = purposes or tuple(MediaPurpose.INVENTORY for _ in statuses)
    assert len(purposes) == len(statuses)
    assets = []
    for index, (status, purpose) in enumerate(zip(statuses, purposes, strict=True)):
        uploaded = status is not MediaAssetStatus.PENDING_UPLOAD
        assets.append(
            MediaAsset(
                id=uuid4(),
                capture_session_id=capture_id,
                room_zone_id=room_zone_id,
                media_purpose=purpose,
                status=status,
                object_key=f"jobs/{job_id}/captures/{capture_id}/{index}",
                content_type="image/jpeg",
                expected_size_bytes=10,
                actual_size_bytes=10 if uploaded else None,
                sha256_hex="a" * 64 if status is MediaAssetStatus.READY else None,
                generation="7" if uploaded else None,
                created_at=NOW + timedelta(seconds=index),
                uploaded_at=NOW if uploaded else None,
            )
        )
    async with factory.begin() as session:
        session.add_all(
            [
                MoveJob(id=job_id, title="capture analysis", status=MoveJobStatus.DRAFT),
                JobParticipant(
                    id=participant_id,
                    job_id=job_id,
                    role=ParticipantRole.CUSTOMER,
                    display_name="owner",
                ),
                Location(
                    id=location_id,
                    job_id=job_id,
                    kind=LocationKind.ORIGIN,
                    label="origin",
                ),
                RoomZone(
                    id=room_zone_id,
                    location_id=location_id,
                    name="living room",
                    sort_order=0,
                ),
                CaptureSession(
                    id=capture_id,
                    job_id=job_id,
                    created_by_participant_id=participant_id,
                    media_consent_policy_version="2026-08-17.v1",
                    privacy_notice_acknowledged=True,
                    media_retention_days=30,
                    media_consented_at=NOW,
                ),
                *assets,
            ]
        )
    return CaptureSeed(
        job_id=job_id,
        participant_id=participant_id,
        capture_id=capture_id,
        room_zone_id=room_zone_id,
        asset_ids=tuple(asset.id for asset in assets),
    )


async def _submit(
    factory: async_sessionmaker[AsyncSession],
    seed: CaptureSeed,
) -> CaptureAnalysisDispatch:
    async with factory.begin() as session:
        response = await submit_capture_analysis(
            session,
            seed.job_id,
            seed.capture_id,
            seed.participant_id,
            trace_id=TRACE_ID,
            now=NOW,
        )
    async with factory() as session:
        row = await session.get(CaptureAnalysisDispatch, response.analysis_run_id)
        assert row is not None
        return row


def _task(row: CaptureAnalysisDispatch, **changes: object) -> AnalysisTaskV1:
    values: dict[str, object] = {
        "analysis_run_id": row.analysis_run_id,
        "capture_session_id": row.capture_session_id,
        "attempt_count": 1,
        "trace_id": row.trace_id,
    }
    values.update(changes)
    return AnalysisTaskV1.model_validate(values)


def _result(seed: CaptureSeed, row: CaptureAnalysisDispatch) -> AnalysisResult:
    return AnalysisResult(
        analysis_run_id=AnalysisRunId(row.analysis_run_id),
        capture_session_id=CaptureSessionId(seed.capture_id),
        model_name="gemini-2.5-flash",
        model_version="2025-08",
        prompt_version="inventory-1",
        draft_items=(
            DraftItem(
                item_key="box",
                description="이삿짐 상자",
                confidence=0.9,
                source_media_asset_ids=(MediaAssetId(seed.asset_ids[0]),),
            ),
        ),
    )


@pytest.mark.anyio
async def test_submit_is_owner_scoped_ready_only_idempotent_and_emits_event(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    seed = await _seed_capture(factory, MediaAssetStatus.READY, MediaAssetStatus.READY)

    async with factory.begin() as session:
        first = await submit_capture_analysis(
            session,
            seed.job_id,
            seed.capture_id,
            seed.participant_id,
            trace_id=TRACE_ID,
            now=NOW,
        )
    async with factory.begin() as session:
        repeated = await submit_capture_analysis(
            session,
            seed.job_id,
            seed.capture_id,
            seed.participant_id,
            trace_id=TRACE_ID,
            now=NOW + timedelta(minutes=1),
        )
        loaded = await get_capture_analysis(
            session,
            seed.job_id,
            seed.capture_id,
            seed.participant_id,
        )

    assert first.analysis_run_id == repeated.analysis_run_id == loaded.analysis_run_id
    assert first.status is CaptureAnalysisStatus.PENDING
    assert first.submitted_at.tzinfo is not None
    async with factory() as session:
        events = (
            await session.scalars(
                select(OutboxEvent).where(OutboxEvent.event_type == "CAPTURE_SUBMITTED_V1")
            )
        ).all()
    assert len(events) == 1
    assert events[0].payload == {
        "capture_session_id": str(seed.capture_id),
        "analysis_run_id": str(first.analysis_run_id),
        "inventory_media_asset_ids": [str(asset_id) for asset_id in seed.asset_ids],
    }


@pytest.mark.anyio
@pytest.mark.parametrize("missing", ["job", "owner", "capture"])
async def test_submit_rejects_missing_or_cross_owner_capture(
    factory: async_sessionmaker[AsyncSession],
    missing: str,
) -> None:
    seed = await _seed_capture(factory, MediaAssetStatus.READY)
    job_id = uuid4() if missing == "job" else seed.job_id
    participant_id = uuid4() if missing == "owner" else seed.participant_id
    capture_id = uuid4() if missing == "capture" else seed.capture_id

    async with factory.begin() as session:
        with pytest.raises(CaptureAnalysisNotFoundError):
            await submit_capture_analysis(
                session,
                job_id,
                capture_id,
                participant_id,
                trace_id=TRACE_ID,
                now=NOW,
            )


@pytest.mark.anyio
@pytest.mark.parametrize("statuses", [(), (MediaAssetStatus.UPLOADED,)])
async def test_submit_requires_nonempty_all_ready_inventory(
    factory: async_sessionmaker[AsyncSession],
    statuses: tuple[MediaAssetStatus, ...],
) -> None:
    seed = await _seed_capture(factory, *statuses)
    async with factory.begin() as session:
        with pytest.raises(CaptureAnalysisConflictError):
            await submit_capture_analysis(
                session,
                seed.job_id,
                seed.capture_id,
                seed.participant_id,
                trace_id=TRACE_ID,
                now=NOW,
            )


@pytest.mark.anyio
async def test_submit_requires_recorded_media_consent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    seed = await _seed_capture(factory, MediaAssetStatus.READY)
    async with factory.begin() as session:
        capture = await session.get(CaptureSession, seed.capture_id)
        assert capture is not None
        capture.media_consent_policy_version = None
        capture.privacy_notice_acknowledged = False
        capture.media_retention_days = None
        capture.media_consented_at = None
    async with factory.begin() as session:
        with pytest.raises(CaptureAnalysisConflictError):
            await submit_capture_analysis(
                session,
                seed.job_id,
                seed.capture_id,
                seed.participant_id,
                trace_id=TRACE_ID,
                now=NOW,
            )


@pytest.mark.anyio
async def test_submit_rejects_closed_job_and_get_is_owner_scoped(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    seed = await _seed_capture(factory, MediaAssetStatus.READY)
    async with factory.begin() as session:
        job = await session.get(MoveJob, seed.job_id)
        assert job is not None
        job.status = MoveJobStatus.CANCELED
    async with factory.begin() as session:
        with pytest.raises(CaptureAnalysisConflictError):
            await submit_capture_analysis(
                session,
                seed.job_id,
                seed.capture_id,
                seed.participant_id,
                trace_id=TRACE_ID,
                now=NOW,
            )
        with pytest.raises(CaptureAnalysisNotFoundError):
            await get_capture_analysis(
                session,
                seed.job_id,
                seed.capture_id,
                seed.participant_id,
            )

    open_seed = await _seed_capture(factory, MediaAssetStatus.READY)
    await _submit(factory, open_seed)
    async with factory.begin() as session:
        for job_id, participant_id in (
            (uuid4(), open_seed.participant_id),
            (open_seed.job_id, uuid4()),
        ):
            with pytest.raises(CaptureAnalysisNotFoundError):
                await get_capture_analysis(
                    session,
                    job_id,
                    open_seed.capture_id,
                    participant_id,
                )


@pytest.mark.anyio
async def test_claim_validates_bounds_and_recovers_expired_lease(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await _seed_capture(factory, MediaAssetStatus.READY)
    second = await _seed_capture(factory, MediaAssetStatus.READY)
    third = await _seed_capture(factory, MediaAssetStatus.READY)
    first_row = await _submit(factory, first)
    second_row = await _submit(factory, second)
    third_row = await _submit(factory, third)
    old_token = uuid4()
    async with factory.begin() as session:
        expired = await session.get(CaptureAnalysisDispatch, second_row.analysis_run_id)
        future = await session.get(CaptureAnalysisDispatch, third_row.analysis_run_id)
        assert expired is not None and future is not None
        expired.status = CaptureAnalysisStatus.DISPATCHING
        expired.dispatch_attempt_count = 1
        expired.last_attempt_at = NOW - timedelta(minutes=2)
        expired.dispatch_token = old_token
        expired.dispatch_locked_until = NOW - timedelta(seconds=1)
        future.scheduled_at = NOW + timedelta(minutes=1)

    async with factory.begin() as session:
        for limit, lease in ((0, 60), (101, 60), (1, 0)):
            with pytest.raises(ValueError):
                await claim_capture_analyses(
                    session,
                    now=NOW,
                    limit=limit,
                    lease_seconds=lease,
                )
        claims = await claim_capture_analyses(session, now=NOW, limit=2, lease_seconds=60)

    assert {claim.task.analysis_run_id for claim in claims} == {
        first_row.analysis_run_id,
        second_row.analysis_run_id,
    }
    async with factory() as session:
        claimed_first = await session.get(CaptureAnalysisDispatch, first_row.analysis_run_id)
        claimed_second = await session.get(CaptureAnalysisDispatch, second_row.analysis_run_id)
        assert claimed_first is not None and claimed_second is not None
        assert claimed_first.dispatch_attempt_count == 1
        assert claimed_second.dispatch_attempt_count == 1
        assert claimed_second.dispatch_token != old_token

    async with factory.begin() as session:
        assert await claim_capture_analyses(session, now=NOW, limit=1) == ()


@pytest.mark.anyio
async def test_finalize_dispatch_handles_success_replay_stale_and_retry(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.modules.analysis_workflow.service.utc_now", lambda: NOW)
    seed = await _seed_capture(factory, MediaAssetStatus.READY)
    row = await _submit(factory, seed)
    async with factory.begin() as session:
        claim = (await claim_capture_analyses(session, now=NOW, limit=1))[0]
        with pytest.raises(ValueError):
            await finalize_capture_analysis_dispatch(session, claim)
        with pytest.raises(ValueError):
            await finalize_capture_analysis_dispatch(
                session,
                claim,
                provider_task_id="task-1",
                error_code="unavailable",
            )
        missing_claim = ClaimedCaptureAnalysis(
            task=_task(row, analysis_run_id=uuid4()),
            dispatch_token=uuid4(),
        )
        assert not await finalize_capture_analysis_dispatch(
            session,
            missing_claim,
            provider_task_id="missing",
        )
        assert await finalize_capture_analysis_dispatch(
            session,
            claim,
            provider_task_id="task-1",
        )
        assert await finalize_capture_analysis_dispatch(
            session,
            claim,
            provider_task_id="task-1",
        )
        assert not await finalize_capture_analysis_dispatch(
            session,
            claim,
            provider_task_id="other-task",
        )

    retry_seed = await _seed_capture(factory, MediaAssetStatus.READY)
    retry_row = await _submit(factory, retry_seed)
    async with factory.begin() as session:
        retry_claim = (await claim_capture_analyses(session, now=NOW, limit=1))[0]
        assert retry_claim.task.analysis_run_id == retry_row.analysis_run_id
        assert await finalize_capture_analysis_dispatch(
            session,
            retry_claim,
            error_code="unavailable",
        )
    async with factory() as session:
        retried = await session.get(CaptureAnalysisDispatch, retry_row.analysis_run_id)
        assert retried is not None
        assert retried.status is CaptureAnalysisStatus.PENDING
        assert retried.last_dispatch_error_code == "unavailable"
        assert retried.scheduled_at.replace(tzinfo=UTC) == NOW + timedelta(seconds=2)


@pytest.mark.anyio
async def test_finalize_accepts_worker_race_and_rejects_lost_lease(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    seed = await _seed_capture(factory, MediaAssetStatus.READY)
    row = await _submit(factory, seed)
    async with factory.begin() as session:
        claim = (await claim_capture_analyses(session, now=NOW, limit=1))[0]
        current = await session.get(CaptureAnalysisDispatch, row.analysis_run_id)
        assert current is not None
        current.status = CaptureAnalysisStatus.RUNNING
        current.dispatch_token = None
        current.dispatch_locked_until = None
        assert await finalize_capture_analysis_dispatch(
            session,
            claim,
            provider_task_id="task-race",
        )

    stale_seed = await _seed_capture(factory, MediaAssetStatus.READY)
    stale_row = await _submit(factory, stale_seed)
    async with factory.begin() as session:
        stale_claim = (await claim_capture_analyses(session, now=NOW, limit=1))[0]
        current = await session.get(CaptureAnalysisDispatch, stale_row.analysis_run_id)
        assert current is not None
        current.dispatch_token = uuid4()
        assert not await finalize_capture_analysis_dispatch(
            session,
            stale_claim,
            error_code="deadline_exceeded",
            now=NOW,
        )


@pytest.mark.anyio
async def test_dispatch_once_validates_settings_and_enqueues_idempotently(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    queue = FakeTaskQueue()
    for timeout, lease in ((0.0, 60), (10.0, 10)):
        with pytest.raises(ValueError):
            await dispatch_capture_analyses_once(
                factory,
                queue,
                queue_name="analysis",
                handler="/tasks/analysis",
                now=NOW,
                lease_seconds=lease,
                enqueue_timeout_seconds=timeout,
            )
    assert await dispatch_capture_analyses_once(
        factory,
        queue,
        queue_name="analysis",
        handler="/tasks/analysis",
        now=NOW,
    ) == AnalysisDispatchResult(claimed=0, queued=0, failed=0)

    seed = await _seed_capture(factory, MediaAssetStatus.READY)
    row = await _submit(factory, seed)
    result = await dispatch_capture_analyses_once(
        factory,
        queue,
        queue_name="analysis",
        handler="/tasks/analysis",
        now=NOW,
    )

    assert result == AnalysisDispatchResult(claimed=1, queued=1, failed=0)
    request = next(iter(queue.requests.values()))
    assert request[1] == "/tasks/analysis"
    assert request[2]["analysis_run_id"] == str(row.analysis_run_id)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (
            ProviderError(
                ProviderErrorKind.DEADLINE_EXCEEDED,
                "timeout",
                retryable=True,
            ),
            "deadline_exceeded",
        ),
        (RuntimeError("unexpected"), "unexpected"),
    ],
)
async def test_dispatch_once_records_sanitized_queue_failure(
    factory: async_sessionmaker[AsyncSession],
    error: Exception,
    expected_code: str,
) -> None:
    seed = await _seed_capture(factory, MediaAssetStatus.READY)
    row = await _submit(factory, seed)
    result = await dispatch_capture_analyses_once(
        factory,
        cast(FakeTaskQueue, QueueFailure(error)),
        queue_name="analysis",
        handler="/tasks/analysis",
        now=NOW,
    )

    assert result == AnalysisDispatchResult(claimed=1, queued=0, failed=1)
    async with factory() as session:
        stored = await session.get(CaptureAnalysisDispatch, row.analysis_run_id)
        assert stored is not None
        assert stored.last_dispatch_error_code == expected_code


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status",
    [
        CaptureAnalysisStatus.PENDING,
        CaptureAnalysisStatus.DISPATCHING,
        CaptureAnalysisStatus.QUEUED,
        CaptureAnalysisStatus.RUNNING,
    ],
)
async def test_start_accepts_all_nonterminal_delivery_states(
    factory: async_sessionmaker[AsyncSession],
    status: CaptureAnalysisStatus,
) -> None:
    seed = await _seed_capture(factory, MediaAssetStatus.READY)
    row = await _submit(factory, seed)
    async with factory.begin() as session:
        stored = await session.get(CaptureAnalysisDispatch, row.analysis_run_id)
        assert stored is not None
        stored.status = status
        if status is CaptureAnalysisStatus.DISPATCHING:
            stored.dispatch_attempt_count = 1
            stored.last_attempt_at = NOW
            stored.dispatch_token = uuid4()
            stored.dispatch_locked_until = NOW + timedelta(minutes=1)
        assert await start_capture_analysis(session, _task(row))
        assert stored.status is CaptureAnalysisStatus.RUNNING
        assert stored.dispatch_token is None


@pytest.mark.anyio
@pytest.mark.parametrize("mismatch", ["missing", "capture", "trace", "attempt"])
async def test_start_rejects_missing_or_stale_tasks(
    factory: async_sessionmaker[AsyncSession],
    mismatch: str,
) -> None:
    seed = await _seed_capture(factory, MediaAssetStatus.READY)
    row = await _submit(factory, seed)
    changes: dict[str, object] = {}
    if mismatch == "missing":
        changes["analysis_run_id"] = uuid4()
    elif mismatch == "capture":
        changes["capture_session_id"] = uuid4()
    elif mismatch == "trace":
        changes["trace_id"] = "f" * 32
    else:
        changes["attempt_count"] = 2
    async with factory.begin() as session:
        with pytest.raises(CaptureAnalysisNotFoundError):
            await start_capture_analysis(session, _task(row, **changes))


@pytest.mark.anyio
async def test_complete_imports_scope_and_terminal_replays_are_idempotent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    seed = await _seed_capture(factory, MediaAssetStatus.READY)
    row = await _submit(factory, seed)
    task = _task(row)
    result = _result(seed, row)
    async with factory.begin() as session:
        assert await start_capture_analysis(session, task)
    async with factory.begin() as session:
        completed = await complete_capture_analysis(
            session,
            task,
            result,
            completed_at=NOW,
        )
    async with factory.begin() as session:
        repeated = await complete_capture_analysis(
            session,
            task,
            result,
            completed_at=NOW + timedelta(minutes=1),
        )
        assert not await start_capture_analysis(session, task)

    assert completed.status is CaptureAnalysisStatus.COMPLETED
    assert completed.scope_version_id is not None
    assert repeated.scope_version_id == completed.scope_version_id
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(ScopeVersion)) == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "ANALYSIS_COMPLETED_V1")
            )
            == 1
        )


@pytest.mark.anyio
async def test_complete_attaches_existing_analysis_scope(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    seed = await _seed_capture(factory, MediaAssetStatus.READY)
    row = await _submit(factory, seed)
    task = _task(row)
    result = _result(seed, row)
    scope_id = uuid4()
    async with factory.begin() as session:
        stored = await session.get(CaptureAnalysisDispatch, row.analysis_run_id)
        assert stored is not None
        stored.status = CaptureAnalysisStatus.RUNNING
        session.add(
            ScopeVersion(
                id=scope_id,
                job_id=seed.job_id,
                sequence_number=1,
                content={"schema_version": 1, "items": []},
                content_hash="a" * 64,
                source_analysis_run_id=row.analysis_run_id,
                source_capture_session_id=seed.capture_id,
                analysis_source=result.model_dump(mode="json"),
                created_by_participant_id=None,
            )
        )
    async with factory.begin() as session:
        response = await complete_capture_analysis(session, task, result, completed_at=NOW)
    assert response.scope_version_id == scope_id


@pytest.mark.anyio
async def test_complete_chains_reanalysis_after_unreviewed_ai_scope(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    seed = await _seed_capture(factory, MediaAssetStatus.READY)
    row = await _submit(factory, seed)
    task = _task(row)
    result = _result(seed, row)
    previous_capture_id = uuid4()
    previous_run_id = uuid4()
    previous_scope_id = uuid4()
    async with factory.begin() as session:
        stored = await session.get(CaptureAnalysisDispatch, row.analysis_run_id)
        assert stored is not None
        stored.status = CaptureAnalysisStatus.RUNNING
        session.add(
            CaptureSession(
                id=previous_capture_id,
                job_id=seed.job_id,
                created_by_participant_id=seed.participant_id,
                media_consent_policy_version="2026-08-17.v1",
                privacy_notice_acknowledged=True,
                media_retention_days=30,
                media_consented_at=NOW,
            )
        )
        session.add(
            ScopeVersion(
                id=previous_scope_id,
                job_id=seed.job_id,
                sequence_number=1,
                content={"schema_version": 1, "items": []},
                content_hash="b" * 64,
                source_analysis_run_id=previous_run_id,
                source_capture_session_id=previous_capture_id,
                analysis_source=result.model_copy(
                    update={
                        "analysis_run_id": previous_run_id,
                        "capture_session_id": previous_capture_id,
                    }
                ).model_dump(mode="json"),
                created_by_participant_id=None,
            )
        )
        session.add(
            CaptureAnalysisDispatch(
                analysis_run_id=previous_run_id,
                capture_session_id=previous_capture_id,
                move_job_id=seed.job_id,
                submitted_by_participant_id=seed.participant_id,
                status=CaptureAnalysisStatus.COMPLETED,
                trace_id="f" * 32,
                scheduled_at=NOW - timedelta(minutes=1),
                scope_version_id=previous_scope_id,
                submitted_at=NOW - timedelta(minutes=1),
                completed_at=NOW - timedelta(minutes=1),
            )
        )

    async with factory.begin() as session:
        response = await complete_capture_analysis(session, task, result, completed_at=NOW)

    assert response.status is CaptureAnalysisStatus.COMPLETED
    assert response.scope_version_id is not None
    async with factory() as session:
        created = await session.get(ScopeVersion, response.scope_version_id)
        assert created is not None
        assert created.parent_version_id == previous_scope_id
        assert created.sequence_number == 2


@pytest.mark.anyio
@pytest.mark.parametrize("invalid_kind", ["empty", "existing_root"])
async def test_complete_turns_unimportable_result_into_manual_fallback(
    factory: async_sessionmaker[AsyncSession],
    invalid_kind: str,
) -> None:
    seed = await _seed_capture(factory, MediaAssetStatus.READY)
    row = await _submit(factory, seed)
    task = _task(row)
    result = _result(seed, row)
    if invalid_kind == "empty":
        result = result.model_copy(update={"draft_items": ()})
    async with factory.begin() as session:
        stored = await session.get(CaptureAnalysisDispatch, row.analysis_run_id)
        assert stored is not None
        stored.status = CaptureAnalysisStatus.RUNNING
        if invalid_kind == "existing_root":
            session.add(
                ScopeVersion(
                    job_id=seed.job_id,
                    sequence_number=1,
                    content={"schema_version": 1, "items": []},
                    content_hash="b" * 64,
                    created_by_participant_id=seed.participant_id,
                )
            )
    async with factory.begin() as session:
        response = await complete_capture_analysis(session, task, result, completed_at=NOW)

    assert response.status is CaptureAnalysisStatus.FAILED
    assert response.failure_code == ProviderErrorKind.INVALID_INPUT
    assert response.retryable is False
    assert response.failure_stage is AnalysisFailureStage.SCOPE_IMPORT
    assert response.provider_status is None
    assert response.failure_detail_code is AnalysisFailureDetail.SCOPE_IMPORT_INVALID
    assert response.scope_version_id is None


@pytest.mark.anyio
async def test_complete_rejects_mismatched_result_and_nonrunning_state(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    seed = await _seed_capture(factory, MediaAssetStatus.READY)
    row = await _submit(factory, seed)
    task = _task(row)
    result = _result(seed, row)
    wrong_run = result.model_copy(update={"analysis_run_id": uuid4()})
    wrong_capture = result.model_copy(update={"capture_session_id": uuid4()})
    async with factory.begin() as session:
        for mismatched in (wrong_run, wrong_capture):
            with pytest.raises(CaptureAnalysisConflictError):
                await complete_capture_analysis(session, task, mismatched, completed_at=NOW)
        with pytest.raises(CaptureAnalysisConflictError):
            await complete_capture_analysis(session, task, result, completed_at=NOW)


@pytest.mark.anyio
async def test_failure_is_idempotent_and_rejects_contradictory_terminal_outcome(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    seed = await _seed_capture(factory, MediaAssetStatus.READY)
    row = await _submit(factory, seed)
    task = _task(row)
    async with factory.begin() as session:
        first = await fail_capture_analysis(
            session,
            task,
            error_kind=ProviderErrorKind.UNAVAILABLE,
            retryable=True,
            failure_stage=AnalysisFailureStage.PROVIDER_CALL,
            provider_status=503,
            failure_detail=AnalysisFailureDetail.PROVIDER_UNAVAILABLE,
            completed_at=NOW,
        )
    async with factory.begin() as session:
        repeated = await fail_capture_analysis(
            session,
            task,
            error_kind=ProviderErrorKind.UNAVAILABLE,
            retryable=True,
            failure_stage=AnalysisFailureStage.PROVIDER_CALL,
            provider_status=503,
            failure_detail=AnalysisFailureDetail.PROVIDER_UNAVAILABLE,
            completed_at=NOW + timedelta(minutes=1),
        )
        with pytest.raises(CaptureAnalysisConflictError):
            await fail_capture_analysis(
                session,
                task,
                error_kind=ProviderErrorKind.PERMISSION_DENIED,
                retryable=True,
                completed_at=NOW,
            )
        with pytest.raises(CaptureAnalysisConflictError):
            await fail_capture_analysis(
                session,
                task,
                error_kind=ProviderErrorKind.UNAVAILABLE,
                retryable=False,
                completed_at=NOW,
            )

    assert first.status is CaptureAnalysisStatus.FAILED
    assert first.failure_stage is AnalysisFailureStage.PROVIDER_CALL
    assert first.provider_status == 503
    assert first.failure_detail_code is AnalysisFailureDetail.PROVIDER_UNAVAILABLE
    assert repeated.completed_at == first.completed_at
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "ANALYSIS_FAILED_V1")
            )
            == 1
        )


@pytest.mark.anyio
async def test_failure_rejects_completed_workflow(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    seed = await _seed_capture(factory, MediaAssetStatus.READY)
    row = await _submit(factory, seed)
    task = _task(row)
    async with factory.begin() as session:
        await start_capture_analysis(session, task)
    async with factory.begin() as session:
        await complete_capture_analysis(session, task, _result(seed, row), completed_at=NOW)
    async with factory.begin() as session:
        with pytest.raises(CaptureAnalysisConflictError):
            await fail_capture_analysis(
                session,
                task,
                error_kind=ProviderErrorKind.CONFLICT,
                retryable=False,
                completed_at=NOW,
            )
