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
    get_analysis_run_status,
    start_analysis_run,
)
from app.platform.db import transactional_session

DEFAULT_ANALYSIS_TIMEOUT_SECONDS = 120.0


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
        status = await get_analysis_run_status(session, analysis_run_id=request.analysis_run_id)
    if status is AnalysisRunStatus.COMPLETED:
        return AnalysisTaskOutcome(AnalysisTaskStatus.SUCCEEDED)
    if status is AnalysisRunStatus.FAILED:
        return AnalysisTaskOutcome(AnalysisTaskStatus.FAILED, retryable=False)

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
