"""Authenticated actor identity passed into application commands."""

from enum import StrEnum
from typing import Annotated

from pydantic import StringConstraints, model_validator

from app.contracts.model import ContractModel
from app.contracts.primitives import JobId, ParticipantId, RequestId, TraceId


class ActorKind(StrEnum):
    """Source that authenticated the request or internal invocation."""

    PARTICIPANT = "participant"
    SERVICE = "service"
    SYSTEM = "system"


class ParticipantRole(StrEnum):
    """Business roles that may be granted for one move job."""

    CUSTOMER = "customer"
    COMPANY_MANAGER = "company_manager"
    FIELD_WORKER = "field_worker"


class ActorContext(ContractModel):
    """Verified identity, tenant boundary, and request correlation."""

    actor_kind: ActorKind
    participant_id: ParticipantId | None = None
    participant_role: ParticipantRole | None = None
    job_id: JobId | None = None
    service_name: (
        Annotated[
            str,
            StringConstraints(min_length=1, max_length=63, pattern=r"^[a-z][a-z0-9-]*[a-z0-9]$"),
        ]
        | None
    ) = None
    request_id: RequestId
    trace_id: TraceId

    @model_validator(mode="after")
    def validate_identity_shape(self) -> "ActorContext":
        """Require exactly the identity fields appropriate for the actor kind."""

        has_participant_identity = all(
            value is not None for value in (self.participant_id, self.participant_role, self.job_id)
        )
        if self.actor_kind is ActorKind.PARTICIPANT:
            if not has_participant_identity or self.service_name is not None:
                msg = "participant actors require participant_id, participant_role, and job_id only"
                raise ValueError(msg)
        elif self.actor_kind is ActorKind.SERVICE:
            has_any_participant_identity = any(
                value is not None
                for value in (self.participant_id, self.participant_role, self.job_id)
            )
            if not self.service_name or has_any_participant_identity:
                msg = "service actors require service_name and no participant identity"
                raise ValueError(msg)
        elif self.service_name is not None or any(
            value is not None for value in (self.participant_id, self.participant_role, self.job_id)
        ):
            msg = "system actors cannot carry participant or service identity"
            raise ValueError(msg)
        return self

    def is_participant_for(self, job_id: JobId) -> bool:
        """Return whether this actor is a verified participant of one job."""

        return self.actor_kind is ActorKind.PARTICIPANT and self.job_id == job_id
