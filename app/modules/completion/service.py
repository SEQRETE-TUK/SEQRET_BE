"""Completion confirmation and append-only audit commands."""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.actor import ParticipantRole
from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.primitives import utc_now
from app.modules.capture.models import CaptureSession, MediaAsset
from app.modules.completion.models import (
    AuditEvent,
    AuditEventType,
    CompletionConfirmation,
    CompletionEvidence,
)
from app.modules.completion.schemas import (
    AuditEventResponse,
    CompletionConfirmationCreate,
    CompletionConfirmationResponse,
    CompletionResult,
)
from app.modules.move_job.models import JobParticipant, MoveJob, MoveJobStatus
from app.modules.scope.models import ChangeRequest, ChangeRequestStatus, ScopeVersion

REQUIRED_COMPLETION_ROLES = (
    ParticipantRole.CUSTOMER,
    ParticipantRole.COMPANY_MANAGER,
)


class CompletionResourceNotFoundError(LookupError):
    """Raised for a missing or cross-job completion resource."""


class CompletionConflictError(ValueError):
    """Raised when completion cannot advance from its current state."""


class CompletionInvalidError(ValueError):
    """Raised when completion evidence violates the media policy."""


def add_audit_event(
    session: AsyncSession,
    job_id: UUID,
    event_type: AuditEventType,
    *,
    actor_participant_id: UUID | None = None,
    payload: Mapping[str, Any] | None = None,
) -> AuditEvent:
    """Append one sanitized business fact inside the caller's transaction."""

    event = AuditEvent(
        job_id=job_id,
        event_type=event_type,
        actor_participant_id=actor_participant_id,
        payload=dict(payload or {}),
        occurred_at=utc_now(),
    )
    session.add(event)
    return event


async def _confirmation_response(
    session: AsyncSession,
    confirmation: CompletionConfirmation,
) -> CompletionConfirmationResponse:
    evidence_ids = (
        await session.scalars(
            select(CompletionEvidence.media_asset_id)
            .where(CompletionEvidence.confirmation_id == confirmation.id)
            .order_by(CompletionEvidence.media_asset_id)
        )
    ).all()
    return CompletionConfirmationResponse(
        id=confirmation.id,
        job_id=confirmation.job_id,
        scope_version_id=confirmation.scope_version_id,
        participant_id=confirmation.participant_id,
        role=confirmation.role,
        evidence_media_asset_ids=tuple(evidence_ids),
        confirmed_at=confirmation.confirmed_at,
    )


async def confirm_completion(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
    role: ParticipantRole,
    command: CompletionConfirmationCreate,
) -> CompletionResult:
    job = await session.scalar(select(MoveJob).where(MoveJob.id == job_id).with_for_update())
    if job is None:
        raise CompletionResourceNotFoundError(job_id)
    if job.status in {MoveJobStatus.COMPLETED, MoveJobStatus.CANCELED}:
        raise CompletionConflictError(job_id)
    participant = await session.scalar(
        select(JobParticipant.id).where(
            JobParticipant.id == participant_id,
            JobParticipant.job_id == job_id,
            JobParticipant.role == role,
            JobParticipant.role.in_(REQUIRED_COMPLETION_ROLES),
        )
    )
    if participant is None:
        raise CompletionResourceNotFoundError(job_id)

    scope_version = await session.scalar(
        select(ScopeVersion).where(
            ScopeVersion.id == command.scope_version_id,
            ScopeVersion.job_id == job_id,
        )
    )
    if scope_version is None:
        raise CompletionResourceNotFoundError(command.scope_version_id)
    if scope_version.locked_at is None:
        raise CompletionConflictError(command.scope_version_id)
    if (
        await session.scalar(
            select(ScopeVersion.id).where(ScopeVersion.parent_version_id == scope_version.id)
        )
        is not None
    ):
        raise CompletionConflictError(command.scope_version_id)
    if (
        await session.scalar(
            select(ChangeRequest.id).where(
                ChangeRequest.job_id == job_id,
                ChangeRequest.status.in_(
                    {
                        ChangeRequestStatus.PENDING,
                        ChangeRequestStatus.CLARIFICATION_REQUESTED,
                    }
                ),
            )
        )
        is not None
    ):
        raise CompletionConflictError(command.scope_version_id)

    existing = await session.scalar(
        select(CompletionConfirmation).where(
            CompletionConfirmation.job_id == job_id,
            CompletionConfirmation.role == role,
        )
    )
    if existing is not None:
        raise CompletionConflictError(job_id)
    other_confirmations = (
        await session.scalars(
            select(CompletionConfirmation).where(CompletionConfirmation.job_id == job_id)
        )
    ).all()
    if any(
        confirmation.scope_version_id != command.scope_version_id
        for confirmation in other_confirmations
    ):
        raise CompletionConflictError(command.scope_version_id)
    if other_confirmations:
        other_evidence_ids = set(
            (
                await session.scalars(
                    select(CompletionEvidence.media_asset_id).where(
                        CompletionEvidence.confirmation_id == other_confirmations[0].id
                    )
                )
            ).all()
        )
        if other_evidence_ids != set(command.evidence_media_asset_ids):
            raise CompletionConflictError(job_id)

    assets = (
        await session.scalars(
            select(MediaAsset)
            .join(CaptureSession, CaptureSession.id == MediaAsset.capture_session_id)
            .join(
                JobParticipant,
                JobParticipant.id == CaptureSession.created_by_participant_id,
            )
            .where(
                MediaAsset.id.in_(command.evidence_media_asset_ids),
                CaptureSession.job_id == job_id,
                JobParticipant.job_id == job_id,
                JobParticipant.role == ParticipantRole.FIELD_WORKER,
                MediaAsset.media_purpose == MediaPurpose.COMPLETION,
                MediaAsset.status.in_({MediaAssetStatus.UPLOADED, MediaAssetStatus.READY}),
            )
        )
    ).all()
    if {asset.id for asset in assets} != set(command.evidence_media_asset_ids):
        raise CompletionInvalidError(job_id)

    now = utc_now()
    confirmation = CompletionConfirmation(
        job_id=job_id,
        scope_version_id=command.scope_version_id,
        participant_id=participant_id,
        role=role,
        confirmed_at=now,
    )
    session.add(confirmation)
    await session.flush()
    session.add_all(
        CompletionEvidence(
            confirmation_id=confirmation.id,
            media_asset_id=media_asset_id,
        )
        for media_asset_id in command.evidence_media_asset_ids
    )
    add_audit_event(
        session,
        job_id,
        AuditEventType.COMPLETION_CONFIRMED,
        actor_participant_id=participant_id,
        payload={
            "completion_confirmation_id": str(confirmation.id),
            "scope_version_id": str(command.scope_version_id),
            "evidence_media_asset_ids": sorted(
                str(media_asset_id) for media_asset_id in command.evidence_media_asset_ids
            ),
        },
    )
    completed_at = None
    if {stored.role for stored in other_confirmations} | {role} == set(REQUIRED_COMPLETION_ROLES):
        job.status = MoveJobStatus.COMPLETED
        job.completed_at = now
        completed_at = now
        add_audit_event(
            session,
            job_id,
            AuditEventType.JOB_COMPLETED,
            actor_participant_id=participant_id,
            payload={"scope_version_id": str(command.scope_version_id)},
        )
    try:
        await session.flush()
    except IntegrityError as error:
        raise CompletionConflictError(job_id) from error
    return CompletionResult(
        confirmation=await _confirmation_response(session, confirmation),
        job_status=job.status,
        completed_at=completed_at,
    )


async def list_completion_confirmations(
    session: AsyncSession,
    job_id: UUID,
) -> tuple[CompletionConfirmationResponse, ...]:
    confirmations = (
        await session.scalars(
            select(CompletionConfirmation)
            .where(CompletionConfirmation.job_id == job_id)
            .order_by(CompletionConfirmation.confirmed_at, CompletionConfirmation.id)
        )
    ).all()
    return tuple(
        [await _confirmation_response(session, confirmation) for confirmation in confirmations]
    )


async def list_audit_events(
    session: AsyncSession,
    job_id: UUID,
) -> tuple[AuditEventResponse, ...]:
    events = (
        await session.scalars(
            select(AuditEvent)
            .where(AuditEvent.job_id == job_id)
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
    ).all()
    return tuple(
        AuditEventResponse(
            id=event.id,
            job_id=event.job_id,
            event_type=event.event_type,
            actor_participant_id=event.actor_participant_id,
            payload=event.payload,
            occurred_at=event.occurred_at,
        )
        for event in events
    )
