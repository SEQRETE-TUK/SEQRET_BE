"""HTTP schemas for media-retention background jobs."""

from datetime import datetime
from uuid import UUID

from pydantic import ConfigDict

from app.contracts.maintenance import BackgroundJobType
from app.contracts.model import ContractModel
from app.modules.background_job.models import BackgroundJobStatus


class BackgroundJobCreate(ContractModel):
    model_config = ConfigDict(strict=False)

    media_asset_id: UUID


class BackgroundJobResponse(ContractModel):
    id: UUID
    job_id: UUID
    media_asset_id: UUID
    job_type: BackgroundJobType
    status: BackgroundJobStatus
    scheduled_at: datetime
    attempt_count: int
    last_error_code: str | None
    created_at: datetime
    last_attempt_at: datetime | None
    execution_deadline_at: datetime | None
    completed_at: datetime | None
