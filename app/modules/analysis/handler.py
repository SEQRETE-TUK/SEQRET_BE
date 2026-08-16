"""Analysis worker handler driving one analysis attempt idempotently.

The handler leases or creates the run, calls the provider-neutral
:class:`AIProviderPort`, and records the outcome through the B-owned analysis
commands. A duplicate task delivery converges on the same terminal outcome
without a second provider call. Reopening a ``FAILED`` run as a fresh attempt is
owned by the retry mechanism and is intentionally out of scope here.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.ai import AnalysisRequest
from app.contracts.ports import AIProviderPort, ProviderError, ProviderErrorKind
from app.contracts.primitives import IdempotencyKey, TraceId
from app.modules.analysis.models import AnalysisRunStatus
from app.modules.analysis.service import (
    complete_analysis_run,
    fail_analysis_run,
    get_analysis_run_snapshot,
    start_analysis_run,
)
from app.platform.db import transactional_session

DEFAULT_ANALYSIS_TIMEOUT_SECONDS = 120.0

# Provider failure kinds whose stored classification should keep being retried.
# Mirrors the Vertex adapter so a redelivered FAILED run recovers its
# retryability from the persisted failure_code across a crash window.
_RETRYABLE_ERROR_KINDS = frozenset(
    {ProviderErrorKind.UNAVAILABLE, ProviderErrorKind.DEADLINE_EXCEEDED}
)


class AnalysisTaskStatus(StrEnum):
    """Terminal outcome of one analysis task delivery."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class AnalysisTaskOutcome:
    """What one delivery resolved to, plus provider retry classification."""

    status: AnalysisTaskStatus
    error_kind: ProviderErrorKind | None = None
    retryable: bool = False


async def handle_analysis_task(
    factory: async_sessionmaker[AsyncSession],
    provider: AIProviderPort,
    request: AnalysisRequest,
    *,
    trace_id: TraceId,
    now: datetime,
    timeout_seconds: float = DEFAULT_ANALYSIS_TIMEOUT_SECONDS,
) -> AnalysisTaskOutcome:
    """Create/lease the run, analyze, and record the outcome exactly once."""

    async with transactional_session(factory) as session:
        await start_analysis_run(
            session,
            analysis_run_id=request.analysis_run_id,
            capture_session_id=request.capture_session_id,
            trace_id=trace_id,
            now=now,
        )

    async with transactional_session(factory) as session:
        snapshot = await get_analysis_run_snapshot(session, analysis_run_id=request.analysis_run_id)
    if snapshot.status is AnalysisRunStatus.COMPLETED:
        return AnalysisTaskOutcome(AnalysisTaskStatus.SUCCEEDED)
    if snapshot.status is AnalysisRunStatus.FAILED:
        # A redelivery after the run was already committed FAILED (e.g. a crash
        # between fail and reopen) must restore the original kind/retryability.
        # Reading status and failure_kind from one snapshot keeps them consistent
        # so a concurrent reopen cannot blank the code between the two reads and
        # misreport a still-retryable failure as non-retryable.
        return AnalysisTaskOutcome(
            AnalysisTaskStatus.FAILED,
            error_kind=snapshot.failure_kind,
            retryable=snapshot.failure_kind in _RETRYABLE_ERROR_KINDS,
        )

    idempotency_key = IdempotencyKey(f"analysis:{request.analysis_run_id}")
    try:
        result = await provider.analyze(
            request=request,
            idempotency_key=idempotency_key,
            timeout_seconds=timeout_seconds,
        )
    except ProviderError as error:
        async with transactional_session(factory) as session:
            await fail_analysis_run(
                session,
                analysis_run_id=request.analysis_run_id,
                error_kind=error.kind,
                now=now,
            )
        return AnalysisTaskOutcome(
            AnalysisTaskStatus.FAILED,
            error_kind=error.kind,
            retryable=error.retryable,
        )

    async with transactional_session(factory) as session:
        await complete_analysis_run(session, result=result, now=now)
    return AnalysisTaskOutcome(AnalysisTaskStatus.SUCCEEDED)
