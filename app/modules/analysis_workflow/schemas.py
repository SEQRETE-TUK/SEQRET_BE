"""HTTP schemas for capture analysis submission and status polling."""

from datetime import datetime
from uuid import UUID

from app.contracts.model import ContractModel
from app.modules.analysis_workflow.models import CaptureAnalysisStatus


class CaptureAnalysisResponse(ContractModel):
    """Provider-neutral status exposed to the capture owner."""

    analysis_run_id: UUID
    capture_session_id: UUID
    status: CaptureAnalysisStatus
    scope_version_id: UUID | None
    failure_code: str | None
    retryable: bool | None
    submitted_at: datetime
    completed_at: datetime | None
