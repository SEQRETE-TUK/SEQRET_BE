"""Authenticated completion confirmation and audit API."""

from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from app.contracts.actor import ParticipantRole
from app.modules.access.auth import CurrentActor, authorize_job_actor
from app.modules.completion.schemas import (
    AuditEventResponse,
    CompletionConfirmationCreate,
    CompletionConfirmationResponse,
    CompletionResult,
)
from app.modules.completion.service import (
    CompletionConflictError,
    CompletionInvalidError,
    CompletionResourceNotFoundError,
    confirm_completion,
    list_audit_events,
    list_completion_confirmations,
)
from app.platform.db.dependencies import Session

router = APIRouter(prefix="/move-jobs", tags=["completion"])
COMPLETION_ROLES = frozenset({ParticipantRole.CUSTOMER, ParticipantRole.COMPANY_MANAGER})


@router.post(
    "/{job_id}/completion-confirmations",
    response_model=CompletionResult,
    status_code=status.HTTP_201_CREATED,
    summary="작업 완료 확인",
)
async def confirm_completion_endpoint(
    job_id: UUID,
    command: CompletionConfirmationCreate,
    actor: CurrentActor,
    session: Session,
) -> CompletionResult:
    authorize_job_actor(actor, job_id, COMPLETION_ROLES)
    try:
        return await confirm_completion(
            session,
            job_id,
            cast(UUID, actor.participant_id),
            cast(ParticipantRole, actor.participant_role),
            command,
        )
    except CompletionResourceNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="completion resource not found",
        ) from error
    except CompletionInvalidError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="completion evidence is invalid",
        ) from error
    except CompletionConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="completion cannot be confirmed",
        ) from error


@router.get(
    "/{job_id}/completion-confirmations",
    response_model=tuple[CompletionConfirmationResponse, ...],
    summary="작업 완료 확인 이력 조회",
)
async def list_completion_confirmations_endpoint(
    job_id: UUID,
    actor: CurrentActor,
    session: Session,
) -> tuple[CompletionConfirmationResponse, ...]:
    authorize_job_actor(actor, job_id)
    return await list_completion_confirmations(session, job_id)


@router.get(
    "/{job_id}/audit-events",
    response_model=tuple[AuditEventResponse, ...],
    summary="작업 감사 이력 조회",
)
async def list_audit_events_endpoint(
    job_id: UUID,
    actor: CurrentActor,
    session: Session,
) -> tuple[AuditEventResponse, ...]:
    authorize_job_actor(actor, job_id)
    return await list_audit_events(session, job_id)
