"""Authenticated completion confirmation and audit API."""

from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.api.errors import protected_error_responses
from app.contracts.actor import ParticipantRole
from app.contracts.ports import ProviderError
from app.modules.access.auth import CurrentActor, authorize_job_actor
from app.modules.capture.router import Storage, storage_error
from app.modules.completion.documents import build_completion_archive
from app.modules.completion.schemas import (
    AuditEventResponse,
    CompletionConfirmationCreate,
    CompletionConfirmationResponse,
    CompletionDecisionCreate,
    CompletionDecisionResponse,
    CompletionRequestCreate,
    CompletionRequestResponse,
    CompletionRequestRevokeCreate,
    CompletionResult,
    CompletionSubmissionCreate,
    CompletionSubmissionResponse,
    CompletionSummaryView,
)
from app.modules.completion.service import (
    CompletionConflictError,
    CompletionInvalidError,
    CompletionResourceNotFoundError,
    confirm_completion,
    create_completion_request,
    decide_completion_request,
    get_completion_summary,
    list_audit_events,
    list_completion_confirmations,
    revoke_completion_request,
    submit_completion,
)
from app.platform.db.dependencies import Session

router = APIRouter(prefix="/move-jobs", tags=["completion"])
COMPLETION_ROLES = frozenset({ParticipantRole.CUSTOMER, ParticipantRole.COMPANY_MANAGER})
FIELD_WORKER_ROLE = frozenset({ParticipantRole.FIELD_WORKER})
COMPANY_ROLE = frozenset({ParticipantRole.COMPANY_MANAGER})
CUSTOMER_ROLE = frozenset({ParticipantRole.CUSTOMER})


def _completion_not_found(error: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="completion resource not found",
    )


def _completion_conflict(error: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="completion state conflict",
    )


@router.post(
    "/{job_id}/completion-submissions",
    response_model=CompletionSubmissionResponse,
    status_code=status.HTTP_201_CREATED,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
    summary="현장기사 작업 완료 기록 제출",
)
async def submit_completion_endpoint(
    job_id: UUID,
    command: CompletionSubmissionCreate,
    response: Response,
    actor: CurrentActor,
    session: Session,
) -> CompletionSubmissionResponse:
    authorize_job_actor(actor, job_id, FIELD_WORKER_ROLE)
    try:
        result = await submit_completion(
            session,
            job_id,
            cast(UUID, actor.participant_id),
            command,
            trace_id=actor.trace_id,
        )
    except CompletionResourceNotFoundError as error:
        raise _completion_not_found(error) from error
    except CompletionInvalidError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="completion evidence is invalid",
        ) from error
    except CompletionConflictError as error:
        raise _completion_conflict(error) from error
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get(
    "/{job_id}/completion-summary",
    response_model=CompletionSummaryView,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ),
    summary="업체·고객 완료와 문서 요약 조회",
)
async def get_completion_summary_endpoint(
    job_id: UUID,
    response: Response,
    actor: CurrentActor,
    session: Session,
    storage: Storage,
) -> CompletionSummaryView:
    authorize_job_actor(actor, job_id, COMPLETION_ROLES)
    try:
        result = await get_completion_summary(
            session,
            storage,
            job_id,
            cast(UUID, actor.participant_id),
            cast(ParticipantRole, actor.participant_role),
        )
    except CompletionResourceNotFoundError as error:
        raise _completion_not_found(error) from error
    except CompletionConflictError as error:
        raise _completion_conflict(error) from error
    except ProviderError as error:
        raise storage_error(error) from error
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/{job_id}/completion-requests",
    response_model=CompletionRequestResponse,
    status_code=status.HTTP_201_CREATED,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
    summary="업체 고객 완료 확인 요청",
)
async def create_completion_request_endpoint(
    job_id: UUID,
    command: CompletionRequestCreate,
    response: Response,
    actor: CurrentActor,
    session: Session,
) -> CompletionRequestResponse:
    authorize_job_actor(actor, job_id, COMPANY_ROLE)
    try:
        result = await create_completion_request(
            session,
            job_id,
            cast(UUID, actor.participant_id),
            command,
            trace_id=actor.trace_id,
        )
    except CompletionResourceNotFoundError as error:
        raise _completion_not_found(error) from error
    except CompletionConflictError as error:
        raise _completion_conflict(error) from error
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/{job_id}/completion-requests/{request_id}/revoke",
    response_model=CompletionRequestResponse,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
    summary="업체 완료 확인 요청 철회",
)
async def revoke_completion_request_endpoint(
    job_id: UUID,
    request_id: UUID,
    command: CompletionRequestRevokeCreate,
    response: Response,
    actor: CurrentActor,
    session: Session,
) -> CompletionRequestResponse:
    authorize_job_actor(actor, job_id, COMPANY_ROLE)
    try:
        result = await revoke_completion_request(
            session,
            job_id,
            request_id,
            cast(UUID, actor.participant_id),
            command.reason,
        )
    except CompletionResourceNotFoundError as error:
        raise _completion_not_found(error) from error
    except CompletionConflictError as error:
        raise _completion_conflict(error) from error
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/{job_id}/completion-requests/{request_id}/decision",
    response_model=CompletionDecisionResponse,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ),
    summary="고객 완료 확인 또는 문제 신고",
)
async def decide_completion_request_endpoint(
    job_id: UUID,
    request_id: UUID,
    command: CompletionDecisionCreate,
    request: Request,
    response: Response,
    actor: CurrentActor,
    session: Session,
) -> CompletionDecisionResponse:
    authorize_job_actor(actor, job_id, CUSTOMER_ROLE)
    retention_days = request.app.state.runtime_context.settings.media_retention_days
    if retention_days is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="media retention policy is unavailable",
        )
    try:
        result = await decide_completion_request(
            session,
            job_id,
            request_id,
            cast(UUID, actor.participant_id),
            command,
            retention_days=retention_days,
            trace_id=actor.trace_id,
        )
    except CompletionResourceNotFoundError as error:
        raise _completion_not_found(error) from error
    except CompletionConflictError as error:
        raise _completion_conflict(error) from error
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get(
    "/{job_id}/documents/archive",
    response_class=Response,
    responses={
        **protected_error_responses(
            status.HTTP_403_FORBIDDEN,
            status.HTTP_404_NOT_FOUND,
            status.HTTP_409_CONFLICT,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ),
        status.HTTP_200_OK: {
            "description": "완료 증빙 PDF ZIP",
            "content": {"application/zip": {}},
        },
    },
    summary="완료 증빙 PDF ZIP 다운로드",
)
async def download_completion_archive_endpoint(
    job_id: UUID,
    actor: CurrentActor,
    session: Session,
    storage: Storage,
) -> Response:
    authorize_job_actor(actor, job_id, COMPANY_ROLE)
    try:
        summary = await get_completion_summary(
            session,
            storage,
            job_id,
            cast(UUID, actor.participant_id),
            ParticipantRole.COMPANY_MANAGER,
        )
        content = build_completion_archive(summary)
    except CompletionResourceNotFoundError as error:
        raise _completion_not_found(error) from error
    except (CompletionConflictError, ValueError) as error:
        raise _completion_conflict(error) from error
    except ProviderError as error:
        raise storage_error(error) from error
    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": (
                f'attachment; filename="seqret-{summary.job.job_code}-documents.zip"'
            ),
        },
    )


@router.post(
    "/{job_id}/completion-confirmations",
    response_model=CompletionResult,
    status_code=status.HTTP_201_CREATED,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ),
    summary="작업 완료 확인",
)
async def confirm_completion_endpoint(
    job_id: UUID,
    command: CompletionConfirmationCreate,
    request: Request,
    actor: CurrentActor,
    session: Session,
) -> CompletionResult:
    authorize_job_actor(actor, job_id, COMPLETION_ROLES)
    retention_days = request.app.state.runtime_context.settings.media_retention_days
    if retention_days is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="media retention policy is unavailable",
        )
    try:
        return await confirm_completion(
            session,
            job_id,
            cast(UUID, actor.participant_id),
            cast(ParticipantRole, actor.participant_role),
            command,
            retention_days=retention_days,
            trace_id=actor.trace_id,
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
    responses=protected_error_responses(status.HTTP_404_NOT_FOUND),
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
    responses=protected_error_responses(status.HTTP_404_NOT_FOUND),
    summary="작업 감사 이력 조회",
)
async def list_audit_events_endpoint(
    job_id: UUID,
    actor: CurrentActor,
    session: Session,
) -> tuple[AuditEventResponse, ...]:
    authorize_job_actor(actor, job_id)
    return await list_audit_events(session, job_id)
