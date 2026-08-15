"""HTTP contracts for dispatch setup, assignment, field brief, and check-in."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.contracts.model import ContractModel
from app.modules.scope_review.schemas import ScopeReviewJobHeader

Label = Annotated[str, Field(min_length=1, max_length=200)]
Reference = Annotated[str, Field(min_length=1, max_length=100)]


class DispatchRequestModel(ContractModel):
    model_config = ConfigDict(strict=False)


class DispatchCheckInItemCreate(DispatchRequestModel):
    key: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    label: Label


class DispatchVehicleCreate(DispatchRequestModel):
    external_reference: Reference
    display_name: Label
    specification: Label
    equipment: Annotated[tuple[Label, ...], Field(max_length=50)] = ()
    capacity_m2: Annotated[int, Field(ge=0, le=1000)]
    available: bool
    conflict_reason: Annotated[str, Field(min_length=1, max_length=500)] | None = None

    @model_validator(mode="after")
    def require_availability_reason_consistency(self) -> Self:
        if self.available == (self.conflict_reason is not None):
            raise ValueError("unavailable vehicles require one conflict reason")
        return self


class DispatchWorkerCreate(DispatchRequestModel):
    external_reference: Reference
    display_name: Label
    role_label: Label
    skills: Annotated[tuple[Label, ...], Field(max_length=100)] = ()
    certifications: Annotated[tuple[Label, ...], Field(max_length=100)] = ()
    available: bool
    conflict_reason: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    participant_id: UUID | None = None

    @model_validator(mode="after")
    def require_availability_reason_consistency(self) -> Self:
        if self.available == (self.conflict_reason is not None):
            raise ValueError("unavailable workers require one conflict reason")
        return self


class DispatchSetupCreate(DispatchRequestModel):
    client_reference: UUID
    source_scope_version_id: UUID
    expected_duration_minutes: Annotated[int, Field(ge=1, le=720)]
    required_vehicle_capacity_m2: Annotated[int, Field(ge=0, le=1000)]
    required_worker_count: Annotated[int, Field(ge=1, le=50)]
    required_skills: Annotated[tuple[Label, ...], Field(max_length=100)] = ()
    required_certifications: Annotated[tuple[Label, ...], Field(max_length=100)] = ()
    check_in_items: Annotated[tuple[DispatchCheckInItemCreate, ...], Field(min_length=1, max_length=20)]
    origin_conditions: Annotated[tuple[Label, ...], Field(max_length=100)] = ()
    safety_notice: Annotated[str, Field(min_length=1, max_length=2000)]
    vehicles: Annotated[tuple[DispatchVehicleCreate, ...], Field(min_length=1, max_length=100)]
    workers: Annotated[tuple[DispatchWorkerCreate, ...], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def require_unique_snapshot_values(self) -> Self:
        collections = (
            self.required_skills,
            self.required_certifications,
            tuple(item.key for item in self.check_in_items),
            tuple(vehicle.external_reference for vehicle in self.vehicles),
            tuple(worker.external_reference for worker in self.workers),
        )
        if any(len(values) != len(set(values)) for values in collections):
            raise ValueError("dispatch snapshot values must be unique")
        participant_ids = tuple(
            worker.participant_id for worker in self.workers if worker.participant_id is not None
        )
        if len(participant_ids) != len(set(participant_ids)):
            raise ValueError("dispatch worker participant IDs must be unique")
        return self


class DispatchVehicleOption(ContractModel):
    id: UUID
    external_reference: str
    display_name: str
    specification: str
    equipment: tuple[str, ...]
    capacity_m2: int
    available: bool
    conflict_reason: str | None


class DispatchWorkerOption(ContractModel):
    id: UUID
    external_reference: str
    display_name: str
    role_label: str
    skills: tuple[str, ...]
    certifications: tuple[str, ...]
    available: bool
    conflict_reason: str | None
    participant_id: UUID | None


class DispatchRequirements(ContractModel):
    start_at: datetime
    expected_duration_minutes: int
    required_vehicle_count: Literal[1] = 1
    required_vehicle_capacity_m2: int
    required_worker_count: int
    required_skills: tuple[str, ...]
    required_certifications: tuple[str, ...]


class DispatchCheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class DispatchCheck(ContractModel):
    key: str
    status: DispatchCheckStatus
    detail: str


class DispatchStatus(StrEnum):
    SETUP_REQUIRED = "setup_required"
    READY = "ready"
    STALE = "stale"
    CONFIRMED = "confirmed"


class DispatchView(ContractModel):
    job: ScopeReviewJobHeader
    setup_id: UUID | None
    dispatch_id: UUID | None
    source_scope_version_id: UUID | None
    source_scope_version_label: str | None
    requirements: DispatchRequirements | None
    vehicle_options: tuple[DispatchVehicleOption, ...]
    worker_options: tuple[DispatchWorkerOption, ...]
    selected_vehicle_id: UUID | None
    selected_worker_ids: tuple[UUID, ...]
    lead_worker_id: UUID | None
    checks: tuple[DispatchCheck, ...]
    worker_note: str | None
    status: DispatchStatus
    confirmed_at: datetime | None
    notification_created: bool


class DispatchConfirmCreate(DispatchRequestModel):
    setup_id: UUID
    vehicle_id: UUID
    lead_worker_id: UUID
    worker_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=50)]
    worker_note: Annotated[str, Field(min_length=1, max_length=2000)] | None = None

    @model_validator(mode="after")
    def require_unique_workers_and_lead(self) -> Self:
        if len(self.worker_ids) != len(set(self.worker_ids)):
            raise ValueError("dispatch worker IDs must be unique")
        if self.lead_worker_id not in self.worker_ids:
            raise ValueError("lead worker must be selected")
        return self


class FieldBriefCheckItem(ContractModel):
    key: str
    label: str
    confirmed: bool


class FieldBriefView(ContractModel):
    job: ScopeReviewJobHeader
    dispatch_id: UUID
    scope_version_id: UUID
    scope_version_label: str
    start_at: datetime
    masked_origin: str | None
    masked_destination: str | None
    lead_worker_name: str
    lead_worker_call_uri: None = None
    company_chat_uri: None = None
    origin_conditions: tuple[str, ...]
    field_check_required_count: int
    check_in_items: tuple[FieldBriefCheckItem, ...]
    assigned_vehicle: DispatchVehicleOption
    assigned_worker_count: int
    required_skills: tuple[str, ...]
    safety_notice: str
    navigation_uri: None = None
    checked_in_at: datetime | None


class FieldCheckInCreate(DispatchRequestModel):
    dispatch_id: UUID
    confirmed_check_keys: Annotated[tuple[str, ...], Field(min_length=1, max_length=20)]

    @model_validator(mode="after")
    def require_unique_keys(self) -> Self:
        if len(self.confirmed_check_keys) != len(set(self.confirmed_check_keys)):
            raise ValueError("check-in keys must be unique")
        return self


class FieldCheckInResponse(ContractModel):
    check_in_id: UUID
    dispatch_id: UUID
    participant_id: UUID
    confirmed_check_keys: tuple[str, ...]
    checked_in_at: datetime
