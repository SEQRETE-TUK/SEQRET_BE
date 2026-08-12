"""Tests for strict shared models and version invariants."""

from datetime import datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.contracts import (
    ActorContext,
    ActorKind,
    AggregateId,
    DomainEvent,
    DomainEventType,
    EventId,
    JobId,
    ParticipantId,
    ParticipantRole,
    RequestId,
)


def _actor_context(**overrides: object) -> ActorContext:
    values: dict[str, object] = {
        "actor_kind": ActorKind.PARTICIPANT,
        "participant_id": ParticipantId(uuid4()),
        "participant_role": ParticipantRole.CUSTOMER,
        "job_id": JobId(uuid4()),
        "request_id": RequestId(uuid4()),
        "trace_id": "0123456789abcdef0123456789abcdef",
    }
    values.update(overrides)
    return ActorContext.model_validate(values)


def test_participant_actor_is_scoped_to_one_job() -> None:
    job_id = JobId(uuid4())
    actor = _actor_context(job_id=job_id)

    assert actor.is_participant_for(job_id)
    assert not actor.is_participant_for(JobId(uuid4()))
    assert "token" not in repr(actor).lower()


@pytest.mark.parametrize(
    "values",
    [
        {"participant_id": None},
        {"participant_role": None},
        {"job_id": None},
        {"service_name": "media-worker"},
    ],
)
def test_participant_actor_requires_complete_verified_identity(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="participant actors require"):
        _actor_context(**values)


def test_service_and_system_actor_shapes_are_distinct() -> None:
    common = {
        "request_id": RequestId(uuid4()),
        "trace_id": "0123456789abcdef0123456789abcdef",
    }

    service = ActorContext.model_validate(
        common | {"actor_kind": ActorKind.SERVICE, "service_name": "media-worker"}
    )
    system = ActorContext.model_validate(common | {"actor_kind": ActorKind.SYSTEM})

    assert not service.is_participant_for(JobId(uuid4()))
    assert system.service_name is None


@pytest.mark.parametrize(
    "values",
    [
        {"actor_kind": ActorKind.SERVICE},
        {"actor_kind": ActorKind.SYSTEM, "service_name": "scheduler"},
        {"actor_kind": ActorKind.SYSTEM, "job_id": JobId(uuid4())},
    ],
)
def test_nonparticipant_actor_rejects_cross_kind_identity(values: dict[str, object]) -> None:
    common = {
        "request_id": RequestId(uuid4()),
        "trace_id": "0123456789abcdef0123456789abcdef",
    }

    with pytest.raises(ValidationError):
        ActorContext.model_validate(common | values)


def test_contract_models_reject_unknown_fields_and_are_immutable() -> None:
    actor = _actor_context()

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ActorContext.model_validate(actor.model_dump() | {"raw_token": "secret"})
    with pytest.raises(ValidationError, match="Instance is frozen"):
        actor.service_name = "changed"  # type: ignore[misc]


def _event(**overrides: object) -> DomainEvent:
    values: dict[str, object] = {
        "event_id": EventId(uuid4()),
        "event_type": DomainEventType.CAPTURE_SUBMITTED_V1,
        "aggregate_id": AggregateId(uuid4()),
        "actor_id": ParticipantId(uuid4()),
        "trace_id": "0123456789abcdef0123456789abcdef",
        "payload": {"capture_session_id": str(uuid4())},
    }
    values.update(overrides)
    return DomainEvent.model_validate(values)


def test_domain_event_serializes_as_versioned_json() -> None:
    event = _event()
    serialized = event.model_dump(mode="json")

    assert UUID(serialized["event_id"]) == event.event_id
    assert serialized["event_type"] == "capture_submitted.v1"
    assert serialized["schema_version"] == 1
    assert event.occurred_at.tzinfo is not None


def test_domain_event_rejects_naive_time_and_version_mismatch() -> None:
    with pytest.raises(ValidationError, match="must include a timezone"):
        _event(occurred_at=datetime(2026, 8, 12, 1, 0, 0))
    with pytest.raises(ValidationError, match="must match"):
        _event(schema_version=2)


def test_domain_event_accepts_explicit_aware_time() -> None:
    occurred_at = datetime.fromisoformat("2026-08-12T01:00:00+09:00")

    assert _event(occurred_at=occurred_at).occurred_at == occurred_at


@pytest.mark.parametrize(
    "trace_id",
    [
        "too-short",
        "ABCDEF0123456789ABCDEF0123456789",
        "g123456789abcdef0123456789abcdef",
    ],
)
def test_trace_id_requires_lowercase_hex(trace_id: str) -> None:
    with pytest.raises(ValidationError):
        _actor_context(trace_id=trace_id)
