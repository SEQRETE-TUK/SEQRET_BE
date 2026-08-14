"""Tests for strict shared models and version invariants."""

from datetime import datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.contracts import (
    ActorContext,
    ActorKind,
    AggregateId,
    BackgroundJobId,
    BackgroundJobType,
    DomainEvent,
    DomainEventType,
    EventId,
    JobId,
    MediaValidationOutcome,
    MediaValidationResultV1,
    MediaValidationTaskV1,
    MediaValidationWorkV1,
    ParticipantId,
    ParticipantRole,
    ProviderErrorKind,
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
        "payload": {
            "capture_session_id": str(uuid4()),
            "analysis_run_id": str(uuid4()),
            "inventory_media_asset_ids": [str(uuid4())],
        },
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
    ("event_type", "payload"),
    [
        (
            DomainEventType.CAPTURE_SUBMITTED_V1,
            {
                "capture_session_id": str(uuid4()),
                "analysis_run_id": str(uuid4()),
                "inventory_media_asset_ids": [str(uuid4())],
            },
        ),
        (
            DomainEventType.ANALYSIS_COMPLETED_V1,
            {
                "capture_session_id": str(uuid4()),
                "analysis_run_id": str(uuid4()),
                "scope_version_id": str(uuid4()),
            },
        ),
        (
            DomainEventType.ANALYSIS_FAILED_V1,
            {
                "capture_session_id": str(uuid4()),
                "analysis_run_id": str(uuid4()),
                "error_kind": "unavailable",
                "retryable": True,
            },
        ),
        (
            DomainEventType.SCOPE_LOCKED_V1,
            {"scope_version_id": str(uuid4()), "content_hash": "a" * 64},
        ),
        (
            DomainEventType.CHANGE_REQUESTED_V1,
            {
                "change_request_id": str(uuid4()),
                "base_scope_version_id": str(uuid4()),
                "evidence_media_asset_ids": [str(uuid4())],
            },
        ),
        (
            DomainEventType.COMPLETION_MEDIA_SUBMITTED_V1,
            {
                "capture_session_id": str(uuid4()),
                "media_asset_id": str(uuid4()),
                "room_zone_id": str(uuid4()),
            },
        ),
        (
            DomainEventType.MEDIA_DELETED_V1,
            {"background_job_id": str(uuid4()), "media_asset_id": str(uuid4())},
        ),
    ],
)
def test_documented_v1_event_payloads_accept_exact_producer_shape(
    event_type: DomainEventType,
    payload: dict[str, object],
) -> None:
    assert _event(event_type=event_type, payload=payload).payload == payload


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (
            DomainEventType.CAPTURE_SUBMITTED_V1,
            {
                "capture_session_id": str(uuid4()),
                "analysis_run_id": str(uuid4()),
                "inventory_media_asset_ids": [],
            },
        ),
        (
            DomainEventType.CAPTURE_SUBMITTED_V1,
            {
                "capture_session_id": str(uuid4()),
                "analysis_run_id": str(uuid4()),
                "inventory_media_asset_ids": [
                    "00000000-0000-4000-8000-000000000000",
                    "00000000-0000-4000-8000-000000000000",
                ],
            },
        ),
        (
            DomainEventType.ANALYSIS_COMPLETED_V1,
            {
                "capture_session_id": str(uuid4()),
                "analysis_run_id": str(uuid4()),
                "scope_version_id": "not-a-uuid",
            },
        ),
        (
            DomainEventType.ANALYSIS_FAILED_V1,
            {
                "capture_session_id": str(uuid4()),
                "analysis_run_id": str(uuid4()),
                "error_kind": "raw-provider-error",
                "retryable": True,
            },
        ),
        (
            DomainEventType.ANALYSIS_FAILED_V1,
            {
                "capture_session_id": str(uuid4()),
                "analysis_run_id": str(uuid4()),
                "error_kind": ["unavailable"],
                "retryable": True,
            },
        ),
        (
            DomainEventType.ANALYSIS_FAILED_V1,
            {
                "capture_session_id": str(uuid4()),
                "analysis_run_id": str(uuid4()),
                "error_kind": "unavailable",
                "retryable": 1,
            },
        ),
        (DomainEventType.SCOPE_LOCKED_V1, {"scope_version_id": str(uuid4())}),
        (
            DomainEventType.CHANGE_REQUESTED_V1,
            {
                "change_request_id": str(uuid4()),
                "base_scope_version_id": str(uuid4()),
                "evidence_media_asset_ids": [1],
            },
        ),
        (
            DomainEventType.CHANGE_REQUESTED_V1,
            {
                "change_request_id": str(uuid4()),
                "base_scope_version_id": str(uuid4()),
                "evidence_media_asset_ids": [],
            },
        ),
        (
            DomainEventType.CHANGE_REQUESTED_V1,
            {
                "change_request_id": str(uuid4()),
                "base_scope_version_id": str(uuid4()),
                "evidence_media_asset_ids": [
                    "00000000-0000-4000-8000-000000000000",
                    "00000000-0000-4000-8000-000000000000",
                ],
            },
        ),
        (
            DomainEventType.SCOPE_LOCKED_V1,
            {
                "scope_version_id": str(uuid4()),
                "content_hash": "a" * 64,
                "signed_url": "https://storage.invalid/read?signature=must-not-leak",
            },
        ),
        (
            DomainEventType.SCOPE_LOCKED_V1,
            {"scope_version_id": str(uuid4()), "content_hash": "A" * 64},
        ),
        (
            DomainEventType.SCOPE_LOCKED_V1,
            {"scope_version_id": "not-a-uuid", "content_hash": "a" * 64},
        ),
        (
            DomainEventType.COMPLETION_MEDIA_SUBMITTED_V1,
            {"capture_session_id": str(uuid4()), "media_asset_id": str(uuid4())},
        ),
        (
            DomainEventType.MEDIA_DELETED_V1,
            {"background_job_id": 1, "media_asset_id": str(uuid4())},
        ),
    ],
)
def test_documented_v1_event_payloads_reject_drift_and_invalid_values(
    event_type: DomainEventType,
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="contract"):
        _event(event_type=event_type, payload=payload)


def test_domain_event_defensively_rejects_an_unhandled_event_type() -> None:
    event = _event()
    object.__setattr__(event, "event_type", "future_event.v1")

    with pytest.raises(ValueError, match="contract"):
        event.require_documented_payload()  # type: ignore[operator]


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


def _validation_result(**overrides: object) -> MediaValidationResultV1:
    values: dict[str, object] = {
        "background_job_id": BackgroundJobId(uuid4()),
        "attempt_count": 1,
        "source_generation": "7",
        "outcome": MediaValidationOutcome.SUCCEEDED,
        "observed_content_type": "image/jpeg",
        "observed_size_bytes": 12,
        "sha256_hex": "a" * 64,
    }
    values.update(overrides)
    return MediaValidationResultV1.model_validate(values)


def test_media_validation_task_and_work_keep_provider_details_scoped() -> None:
    background_job_id = BackgroundJobId(uuid4())
    task = MediaValidationTaskV1(
        background_job_id=background_job_id,
        attempt_count=2,
        trace_id="0123456789abcdef0123456789abcdef",
    )
    work = MediaValidationWorkV1(
        background_job_id=background_job_id,
        attempt_count=2,
        object_key="jobs/1/captures/1/photo.jpg",
        source_generation="7",
        expected_content_type="image/jpeg",
        expected_size_bytes=12,
    )

    assert task.model_dump(mode="json") == {
        "schema_version": 1,
        "background_job_id": str(background_job_id),
        "job_type": BackgroundJobType.MEDIA_VALIDATION.value,
        "attempt_count": 2,
        "trace_id": "0123456789abcdef0123456789abcdef",
    }
    assert work.model_dump(mode="json") == {
        "schema_version": 1,
        "background_job_id": str(background_job_id),
        "attempt_count": 2,
        "object_key": "jobs/1/captures/1/photo.jpg",
        "source_generation": "7",
        "expected_content_type": "image/jpeg",
        "expected_size_bytes": 12,
    }
    assert "photo.jpg" not in repr(work)


def test_media_validation_result_accepts_exact_success_and_failure_shapes() -> None:
    succeeded = _validation_result()
    failed = _validation_result(
        outcome=MediaValidationOutcome.FAILED,
        observed_content_type=None,
        observed_size_bytes=None,
        sha256_hex=None,
        error_kind=ProviderErrorKind.INVALID_INPUT,
    )

    assert succeeded.model_dump(mode="json") == {
        "schema_version": 1,
        "background_job_id": str(succeeded.background_job_id),
        "attempt_count": 1,
        "source_generation": "7",
        "outcome": "succeeded",
        "observed_content_type": "image/jpeg",
        "observed_size_bytes": 12,
        "sha256_hex": "a" * 64,
        "error_kind": None,
    }
    assert failed.model_dump(mode="json") == {
        "schema_version": 1,
        "background_job_id": str(failed.background_job_id),
        "attempt_count": 1,
        "source_generation": "7",
        "outcome": "failed",
        "observed_content_type": None,
        "observed_size_bytes": None,
        "sha256_hex": None,
        "error_kind": "invalid_input",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"observed_content_type": None},
        {"error_kind": ProviderErrorKind.CONFLICT},
        {
            "outcome": MediaValidationOutcome.FAILED,
            "observed_content_type": None,
            "observed_size_bytes": None,
            "sha256_hex": None,
        },
        {
            "outcome": MediaValidationOutcome.FAILED,
            "error_kind": ProviderErrorKind.INVALID_INPUT,
        },
    ],
)
def test_media_validation_result_rejects_mixed_terminal_shapes(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="media validation results require"):
        _validation_result(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_generation": ""},
        {"source_generation": " "},
        {"source_generation": "7" * 256},
        {"sha256_hex": "a" * 63},
        {"sha256_hex": "A" * 64},
        {"sha256_hex": "g" * 64},
    ],
)
def test_media_validation_result_rejects_invalid_generation_and_hash(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _validation_result(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_generation": " 7"},
        {"observed_content_type": "image/jpeg "},
        {"sha256_hex": f"{'a' * 64} "},
    ],
)
def test_media_validation_result_rejects_padded_provider_values_before_normalization(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="must not contain surrounding whitespace"):
        _validation_result(**overrides)


def test_media_validation_result_rejects_non_object_input() -> None:
    with pytest.raises(ValidationError):
        MediaValidationResultV1.model_validate([])
