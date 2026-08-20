"""HTTP schemas and provider-neutral view mapping for capture analysis."""

from datetime import UTC, datetime
from uuid import UUID

from app.contracts.ai import AnalysisFailureDetail, AnalysisFailureStage
from app.contracts.model import ContractModel
from app.modules.analysis_workflow.models import (
    CaptureAnalysisDispatch,
    CaptureAnalysisStatus,
)


class CaptureAnalysisResponse(ContractModel):
    """Provider-neutral status exposed to the capture owner."""

    analysis_run_id: UUID
    capture_session_id: UUID
    status: CaptureAnalysisStatus
    scope_version_id: UUID | None
    failure_code: str | None
    retryable: bool | None
    failure_stage: AnalysisFailureStage | None
    provider_status: int | None
    failure_detail_code: AnalysisFailureDetail | None
    submitted_at: datetime
    completed_at: datetime | None


def capture_analysis_response(row: CaptureAnalysisDispatch) -> CaptureAnalysisResponse:
    """Build the public view without exposing queue or provider internals."""

    submitted_at = row.submitted_at
    completed_at = row.completed_at
    return CaptureAnalysisResponse(
        analysis_run_id=row.analysis_run_id,
        capture_session_id=row.capture_session_id,
        status=row.status,
        scope_version_id=row.scope_version_id,
        failure_code=row.failure_code,
        retryable=row.retryable,
        failure_stage=(
            AnalysisFailureStage(row.failure_stage) if row.failure_stage is not None else None
        ),
        provider_status=row.provider_status,
        failure_detail_code=(
            AnalysisFailureDetail(row.failure_detail_code)
            if row.failure_detail_code is not None
            else None
        ),
        submitted_at=(
            submitted_at.replace(tzinfo=UTC) if submitted_at.tzinfo is None else submitted_at
        ),
        completed_at=(
            completed_at.replace(tzinfo=UTC)
            if completed_at is not None and completed_at.tzinfo is None
            else completed_at
        ),
    )
