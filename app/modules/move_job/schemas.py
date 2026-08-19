"""Request and response schemas for move job commands."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.contracts.actor import ParticipantRole
from app.contracts.model import ContractModel
from app.contracts.scope_review import (
    CompanyParticipationStatus,
    QuoteSnapshot,
    ScopeReviewStatus,
)
from app.modules.access.schemas import AccessLinkResponse
from app.modules.completion.models import CompletionRequestStatus
from app.modules.move_job.models import LocationKind, MoveJobStatus


class RequestModel(ContractModel):
    """Strict-shaped API input with JSON-compatible coercion."""

    model_config = ConfigDict(strict=False)


class ParticipantCreate(RequestModel):
    """Participant connected while creating a job."""

    role: ParticipantRole
    display_name: Annotated[str, Field(min_length=1, max_length=100)]


class RoomZoneCreate(RequestModel):
    """Location subdivision ordered for client rendering."""

    name: Annotated[str, Field(min_length=1, max_length=100)]
    sort_order: Annotated[int, Field(ge=0)]


class KnowledgeStatus(StrEnum):
    """Whether a numeric work condition is known by the user."""

    KNOWN = "known"
    UNKNOWN = "unknown"


class ResidenceType(StrEnum):
    APARTMENT = "apartment"
    VILLA = "villa"
    OFFICETEL = "officetel"
    HOUSE = "house"
    STUDIO = "studio"
    OTHER = "other"
    UNKNOWN = "unknown"


class ElevatorAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class StairUsage(StrEnum):
    REQUIRED = "required"
    NOT_REQUIRED = "not_required"
    UNKNOWN = "unknown"


class LadderTruckUsage(StrEnum):
    """Whether a ladder truck is planned for one endpoint."""

    REQUIRED = "required"
    NOT_REQUIRED = "not_required"
    UNKNOWN = "unknown"


class ParkingAccess(StrEnum):
    AVAILABLE = "available"
    RESTRICTED = "restricted"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class FloorCondition(RequestModel):
    status: KnowledgeStatus = KnowledgeStatus.UNKNOWN
    value: Annotated[int, Field(ge=-10, le=200)] | None = None

    @model_validator(mode="after")
    def require_value_to_match_status(self) -> Self:
        if (self.status is KnowledgeStatus.KNOWN) != (self.value is not None):
            raise ValueError("known floor requires a value and unknown floor forbids one")
        return self


class CarryDistanceCondition(RequestModel):
    status: KnowledgeStatus = KnowledgeStatus.UNKNOWN
    value_m: Annotated[int, Field(ge=0, le=100_000)] | None = None

    @model_validator(mode="after")
    def require_value_to_match_status(self) -> Self:
        if (self.status is KnowledgeStatus.KNOWN) != (self.value_m is not None):
            raise ValueError(
                "known carry distance requires a value and unknown distance forbids one"
            )
        return self


class LocationConditions(RequestModel):
    """Structured quote-impacting conditions for one endpoint."""

    residence_type: ResidenceType = ResidenceType.UNKNOWN
    floor: FloorCondition = Field(default_factory=FloorCondition)
    elevator: ElevatorAvailability = ElevatorAvailability.UNKNOWN
    stairs: StairUsage = StairUsage.UNKNOWN
    ladder: LadderTruckUsage = LadderTruckUsage.UNKNOWN
    parking_access: ParkingAccess = ParkingAccess.UNKNOWN
    carry_distance: CarryDistanceCondition = Field(default_factory=CarryDistanceCondition)
    access_note: Annotated[str, Field(min_length=1, max_length=1000)] | None = None


class LocationCreate(RequestModel):
    """Origin or destination with separately recoverable address fields."""

    kind: LocationKind
    label: Annotated[str, Field(min_length=1, max_length=100)]
    detail_address: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    conditions: LocationConditions = Field(default_factory=LocationConditions)
    room_zones: Annotated[
        tuple[RoomZoneCreate, ...],
        Field(min_length=1, max_length=100),
    ]

    @model_validator(mode="after")
    def require_unique_room_zones(self) -> "LocationCreate":
        names = [zone.name for zone in self.room_zones]
        orders = [zone.sort_order for zone in self.room_zones]
        if len(names) != len(set(names)):
            raise ValueError("room zone names must be unique within a location")
        if len(orders) != len(set(orders)):
            raise ValueError("room zone sort orders must be unique within a location")
        return self


class MoveJobCreate(RequestModel):
    """Atomic command that creates the root and initial topology."""

    title: Annotated[str, Field(min_length=1, max_length=200)]
    scheduled_at: datetime | None = None
    participants: Annotated[
        tuple[ParticipantCreate, ...],
        Field(min_length=len(ParticipantRole), max_length=len(ParticipantRole)),
    ]
    locations: Annotated[tuple[LocationCreate, ...], Field(min_length=1, max_length=2)]

    @model_validator(mode="after")
    def require_unique_roles_and_locations(self) -> "MoveJobCreate":
        roles = [participant.role for participant in self.participants]
        kinds = [location.kind for location in self.locations]
        if len(roles) != len(set(roles)):
            raise ValueError("participant roles must be unique within a job")
        if len(kinds) != len(set(kinds)):
            raise ValueError("location kinds must be unique within a job")
        if self.scheduled_at is not None and self.scheduled_at.utcoffset() is None:
            raise ValueError("scheduled_at must include a timezone")
        return self


class CustomerMoveJobCreate(RequestModel):
    """Self-service command that grants only the creating customer capability."""

    title: Annotated[str, Field(min_length=1, max_length=200)]
    scheduled_at: datetime | None = None
    customer_display_name: Annotated[str, Field(min_length=1, max_length=100)]
    locations: Annotated[tuple[LocationCreate, ...], Field(min_length=1, max_length=2)]

    @model_validator(mode="after")
    def require_unique_locations_and_aware_time(self) -> "CustomerMoveJobCreate":
        kinds = [location.kind for location in self.locations]
        if len(kinds) != len(set(kinds)):
            raise ValueError("location kinds must be unique within a job")
        if self.scheduled_at is not None and self.scheduled_at.utcoffset() is None:
            raise ValueError("scheduled_at must include a timezone")
        return self


class ParticipantResponse(ContractModel):
    id: UUID
    role: ParticipantRole
    display_name: str


class RoomZoneResponse(ContractModel):
    id: UUID
    name: str
    sort_order: int


class LocationResponse(ContractModel):
    id: UUID
    kind: LocationKind
    label: str
    detail_address: str | None
    conditions: LocationConditions
    room_zones: tuple[RoomZoneResponse, ...]


class MoveJobResponse(ContractModel):
    id: UUID
    title: str
    status: MoveJobStatus
    scheduled_at: datetime | None
    created_at: datetime
    completed_at: datetime | None
    participants: tuple[ParticipantResponse, ...]
    locations: tuple[LocationResponse, ...]


class MoveJobLocationPatch(RequestModel):
    """Mutable quote-impacting fields for one existing endpoint."""

    kind: LocationKind
    label: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    detail_address: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    conditions: LocationConditions | None = None

    @model_validator(mode="after")
    def require_one_mutable_field(self) -> "MoveJobLocationPatch":
        if (
            self.label is None
            and "detail_address" not in self.model_fields_set
            and self.conditions is None
        ):
            raise ValueError("location patch requires label, detail_address, or conditions")
        return self


class MoveJobPatch(RequestModel):
    """Partial basic-information update before a company quote exists."""

    title: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    scheduled_at: datetime | None = None
    locations: (
        Annotated[tuple[MoveJobLocationPatch, ...], Field(min_length=1, max_length=2)] | None
    ) = None

    @model_validator(mode="after")
    def require_fields_and_aware_schedule(self) -> "MoveJobPatch":
        if not self.model_fields_set:
            raise ValueError("move job patch requires at least one field")
        if "title" in self.model_fields_set and self.title is None:
            raise ValueError("title cannot be null")
        if "locations" in self.model_fields_set and self.locations is None:
            raise ValueError("locations cannot be null")
        if self.scheduled_at is not None and self.scheduled_at.utcoffset() is None:
            raise ValueError("scheduled_at must include a timezone")
        if self.locations is not None:
            kinds = [location.kind for location in self.locations]
            if len(kinds) != len(set(kinds)):
                raise ValueError("location kinds must be unique")
        return self


class MoveJobSummaryResponse(ContractModel):
    """Role-neutral list item matching the frontend move summary contract."""

    job: MoveJobResponse
    version_label: str
    scope_status: ScopeReviewStatus
    company_participation_status: CompanyParticipationStatus
    completion_request_status: CompletionRequestStatus | None
    quote: QuoteSnapshot | None
    item_count: int
    adjustment_count: int


class MoveJobListResponse(ContractModel):
    moves: tuple[MoveJobSummaryResponse, ...]
    next_cursor: str | None = None


class MoveJobCreatedResponse(ContractModel):
    job: MoveJobResponse
    access_links: tuple[AccessLinkResponse, ...]


class CustomerMoveJobCreatedResponse(ContractModel):
    """Self-service creation result with no capability for another role."""

    job: MoveJobResponse
    customer_access_link: AccessLinkResponse
