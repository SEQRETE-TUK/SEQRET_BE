"""Completion commands and audit timeline responses."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, JsonValue, model_validator

from app.contracts.actor import ParticipantRole
from app.contracts.model import ContractModel
from app.modules.completion.models import (
    AuditEventType,
    CompletionProblemType,
    CompletionRequestStatus,
)
from app.modules.move_job.models import MoveJobStatus
from app.modules.scope_review.schemas import QuoteSnapshot, ScopeReviewJobHeader


class CompletionRequestModel(ContractModel):
    model_config = ConfigDict(strict=False)


class CompletionConfirmationCreate(CompletionRequestModel):
    scope_version_id: UUID
    evidence_media_asset_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def require_unique_evidence(self) -> "CompletionConfirmationCreate":
        if len(self.evidence_media_asset_ids) != len(set(self.evidence_media_asset_ids)):
            raise ValueError("completion evidence IDs must be unique")
        return self


class CompletionConfirmationResponse(ContractModel):
    id: UUID
    job_id: UUID
    scope_version_id: UUID
    participant_id: UUID
    role: ParticipantRole
    evidence_media_asset_ids: tuple[UUID, ...]
    confirmed_at: datetime


class CompletionResult(ContractModel):
    confirmation: CompletionConfirmationResponse
    job_status: MoveJobStatus
    completed_at: datetime | None


class CompletionWorkerShiftCreate(CompletionRequestModel):
    worker_id: UUID
    started_at: datetime
    ended_at: datetime

    @model_validator(mode="after")
    def require_aware_ordered_times(self) -> Self:
        for value in (self.started_at, self.ended_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("worker shift timestamps must include a timezone")
        if self.ended_at < self.started_at:
            raise ValueError("worker shift must end after it starts")
        return self


class CompletionSubmissionCreate(CompletionRequestModel):
    client_reference: UUID
    dispatch_id: UUID
    scope_version_id: UUID
    completion_media_asset_ids: Annotated[tuple[UUID, ...], Field(max_length=50)] = ()
    completed_check_keys: Annotated[tuple[str, ...], Field(min_length=1, max_length=20)]
    worker_shifts: Annotated[
        tuple[CompletionWorkerShiftCreate, ...], Field(min_length=1, max_length=50)
    ]
    onsite_customer_confirmed: Literal[True]
    onsite_confirmed_at: datetime
    work_ended_at: datetime

    @model_validator(mode="after")
    def require_unique_values_and_aware_times(self) -> Self:
        collections = (
            self.completion_media_asset_ids,
            self.completed_check_keys,
            tuple(shift.worker_id for shift in self.worker_shifts),
        )
        if any(len(values) != len(set(values)) for values in collections):
            raise ValueError("completion submission values must be unique")
        for value in (self.onsite_confirmed_at, self.work_ended_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("completion timestamps must include a timezone")
        return self


class CompletionWorkerShift(ContractModel):
    worker_id: UUID
    external_reference: str
    display_name: str
    role_label: str
    started_at: datetime
    ended_at: datetime
    duration_minutes: int


class CompletionSubmissionResponse(ContractModel):
    completion_submission_id: UUID
    client_reference: UUID
    job_id: UUID
    dispatch_id: UUID
    scope_version_id: UUID
    submitted_by_participant_id: UUID
    completion_media_asset_ids: tuple[UUID, ...]
    completed_check_keys: tuple[str, ...]
    worker_shifts: tuple[CompletionWorkerShift, ...]
    onsite_customer_confirmed: bool
    onsite_confirmed_at: datetime
    work_ended_at: datetime
    submitted_at: datetime


class CompletionRequestCreate(CompletionRequestModel):
    client_reference: UUID
    completion_submission_id: UUID


class CompletionRequestViewStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    REQUESTED = "requested"
    CONFIRMED = "confirmed"
    ISSUE_REPORTED = "issue_reported"
    REVOKED = "revoked"
    EXPIRED = "expired"


class CompletionProblemReportResponse(ContractModel):
    problem_report_id: UUID
    problem_type: CompletionProblemType
    description: str
    reported_at: datetime


class CompletionRequestResponse(ContractModel):
    completion_request_id: UUID
    client_reference: UUID
    completion_submission_id: UUID
    status: CompletionRequestViewStatus
    requested_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    decided_at: datetime | None
    unrecorded_extra_charge: bool | None
    problem_report: CompletionProblemReportResponse | None
    notification_created: bool


class CompletionRequestRevokeCreate(CompletionRequestModel):
    reason: Annotated[str, Field(min_length=1, max_length=2000)]


class CompletionDecisionCreate(CompletionRequestModel):
    decision: Literal["confirm", "report_issue"]
    problem_type: CompletionProblemType | None = None
    problem_description: Annotated[str, Field(min_length=1, max_length=2000)] | None = None
    unrecorded_extra_charge: bool | None = None

    @model_validator(mode="after")
    def require_problem_fields_only_for_reports(self) -> Self:
        if self.decision == "report_issue":
            if self.problem_type is None or self.problem_description is None:
                raise ValueError("problem reports require a type and description")
        elif self.problem_type is not None or self.problem_description is not None:
            raise ValueError("completion confirmation must not contain problem fields")
        return self


class CompletionDecisionResponse(ContractModel):
    completion_request_id: UUID
    decision: Literal["confirm", "report_issue"]
    status: CompletionRequestStatus
    job_status: MoveJobStatus
    completed_at: datetime | None
    decided_at: datetime
    problem_report: CompletionProblemReportResponse | None
    retention_scheduled_count: int


class CompletionChecklistSummary(ContractModel):
    completed_count: int
    total_count: int


class CompletionMediaPreview(ContractModel):
    media_asset_id: UUID
    room_zone_id: UUID
    room_zone_label: str
    content_type: str
    read_url: Annotated[
        str,
        Field(min_length=1, repr=False, json_schema_extra={"format": "uri"}),
    ]
    expires_at: datetime


class CompletionFieldChangeSummary(ContractModel):
    proposal_id: UUID
    title: str
    status: str
    amount_delta_krw: int
    total_amount_krw: int
    decided_at: datetime | None


class CompletionDocumentStatus(StrEnum):
    READY = "ready"
    NOT_READY = "not_ready"


class CompletionDocumentSummary(ContractModel):
    key: Literal["quote", "changes", "completion", "decision"]
    name: str
    status: CompletionDocumentStatus


class CompletionSummaryView(ContractModel):
    job: ScopeReviewJobHeader
    job_status: MoveJobStatus
    completion_submission_id: UUID | None
    completed_at: datetime | None
    final_amount_krw: int | None
    duration_minutes: int | None
    completion_media: tuple[CompletionMediaPreview, ...]
    completion_media_count: int
    checklist: CompletionChecklistSummary
    onsite_confirmation_completed: bool
    worker_shifts: tuple[CompletionWorkerShift, ...]
    field_changes: tuple[CompletionFieldChangeSummary, ...]
    quote: QuoteSnapshot | None
    completion_request: CompletionRequestResponse | None
    approved_scope_version_id: UUID | None
    approved_scope_version_label: str | None
    documents: tuple[CompletionDocumentSummary, ...]
    archive_ready: bool
    retention_until: datetime | None
    problem_report_count: int


class AuditEventResponse(ContractModel):
    id: UUID
    job_id: UUID
    event_type: AuditEventType
    actor_participant_id: UUID | None
    payload: dict[str, JsonValue]
    occurred_at: datetime
