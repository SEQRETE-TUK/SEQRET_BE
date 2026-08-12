"""Request and response schemas for move job commands."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.contracts.actor import ParticipantRole
from app.contracts.model import ContractModel
from app.modules.access.schemas import AccessLinkResponse
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


class LocationCreate(RequestModel):
    """Origin or destination without a raw address."""

    kind: LocationKind
    label: Annotated[str, Field(min_length=1, max_length=100)]
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


class MoveJobCreatedResponse(ContractModel):
    job: MoveJobResponse
    access_links: tuple[AccessLinkResponse, ...]
