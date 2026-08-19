"""Frontend-facing scope review and quote routes."""

from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status

from app.api.errors import protected_error_responses
from app.contracts.actor import ParticipantRole
from app.contracts.ports import ProviderError
from app.modules.access.auth import CurrentActor, authorize_job_actor
from app.modules.capture.router import Storage, storage_error
from app.modules.scope.service import (
    ScopeApprovalConflictError,
    ScopeResourceNotFoundError,
    ScopeVersionConflictError,
)
from app.modules.scope_review.schemas import (
    ScopeConfirmationHistoryView,
    ScopeConfirmCreate,
    ScopeConfirmResponse,
    ScopeProposalCreate,
    ScopeProposalResponse,
    ScopeReviewView,
    ScopeRevisionRequestCreate,
    ScopeRevisionRequestResponse,
)
from app.modules.scope_review.service import (
    ScopeReviewConflictError,
    ScopeReviewNotFoundError,
    confirm_scope_proposal,
    create_scope_proposal,
    get_scope_confirmation_history,
    get_scope_review,
    request_scope_revision,
)
from app.platform.db.dependencies import Session

router = APIRouter(prefix="/move-jobs", tags=["scope-review"])
REVIEW_ROLES = frozenset(
    {
        ParticipantRole.CUSTOMER,
        ParticipantRole.COMPANY_MANAGER,
        ParticipantRole.FIELD_WORKER,
    }
)
CONFIRMATION_HISTORY_ROLES = frozenset({ParticipantRole.CUSTOMER, ParticipantRole.COMPANY_MANAGER})


def _not_found(error: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="scope review resource not found",
    )


def _conflict(error: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="scope review state conflict",
    )


@router.get(
    "/{job_id}/scope-review",
    response_model=ScopeReviewView,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ),
    summary="현재 작업범위와 견적 검토 화면 조회",
)
async def get_scope_review_endpoint(
    job_id: UUID,
    response: Response,
    actor: CurrentActor,
    session: Session,
    storage: Storage,
) -> ScopeReviewView:
    authorize_job_actor(actor, job_id, REVIEW_ROLES)
    try:
        view = await get_scope_review(
            session,
            storage,
            job_id,
            cast(UUID, actor.participant_id),
            cast(ParticipantRole, actor.participant_role),
        )
    except ScopeReviewNotFoundError as error:
        raise _not_found(error) from error
    except ScopeReviewConflictError as error:
        raise _conflict(error) from error
    except ProviderError as error:
        raise storage_error(error) from error
    response.headers["Cache-Control"] = "no-store"
    return view


@router.get(
    "/{job_id}/scope-review/history",
    response_model=ScopeConfirmationHistoryView,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
    ),
    summary="버전별 범위·견적·공동확인 이력 조회",
)
async def get_scope_confirmation_history_endpoint(
    job_id: UUID,
    response: Response,
    actor: CurrentActor,
    session: Session,
) -> ScopeConfirmationHistoryView:
    authorize_job_actor(actor, job_id, CONFIRMATION_HISTORY_ROLES)
    try:
        result = await get_scope_confirmation_history(
            session,
            job_id,
            cast(UUID, actor.participant_id),
            cast(ParticipantRole, actor.participant_role),
        )
    except ScopeReviewNotFoundError as error:
        raise _not_found(error) from error
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/{job_id}/scope-proposals",
    response_model=ScopeProposalResponse,
    status_code=status.HTTP_201_CREATED,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
    summary="업체 작업범위와 견적 제안",
)
async def create_scope_proposal_endpoint(
    job_id: UUID,
    command: ScopeProposalCreate,
    response: Response,
    actor: CurrentActor,
    session: Session,
) -> ScopeProposalResponse:
    authorize_job_actor(actor, job_id, frozenset({ParticipantRole.COMPANY_MANAGER}))
    try:
        result = await create_scope_proposal(
            session,
            job_id,
            cast(UUID, actor.participant_id),
            command,
        )
    except (ScopeReviewNotFoundError, ScopeResourceNotFoundError) as error:
        raise _not_found(error) from error
    except (
        ScopeReviewConflictError,
        ScopeVersionConflictError,
        ScopeApprovalConflictError,
    ) as error:
        raise _conflict(error) from error
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/{job_id}/scope-review/revision-request",
    response_model=ScopeRevisionRequestResponse,
    status_code=status.HTTP_201_CREATED,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
    summary="고객 작업범위 수정 요청",
)
async def request_scope_revision_endpoint(
    job_id: UUID,
    command: ScopeRevisionRequestCreate,
    response: Response,
    actor: CurrentActor,
    session: Session,
) -> ScopeRevisionRequestResponse:
    authorize_job_actor(actor, job_id, frozenset({ParticipantRole.CUSTOMER}))
    try:
        result = await request_scope_revision(
            session,
            job_id,
            cast(UUID, actor.participant_id),
            command,
        )
    except ScopeReviewNotFoundError as error:
        raise _not_found(error) from error
    except ScopeReviewConflictError as error:
        raise _conflict(error) from error
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/{job_id}/scope-review/confirm",
    response_model=ScopeConfirmResponse,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
    summary="고객 현재 견적 범위 확인",
)
async def confirm_scope_proposal_endpoint(
    job_id: UUID,
    command: ScopeConfirmCreate,
    response: Response,
    actor: CurrentActor,
    session: Session,
) -> ScopeConfirmResponse:
    authorize_job_actor(actor, job_id, frozenset({ParticipantRole.CUSTOMER}))
    try:
        result = await confirm_scope_proposal(
            session,
            job_id,
            cast(UUID, actor.participant_id),
            command.scope_version_id,
            trace_id=actor.trace_id,
        )
    except (ScopeReviewNotFoundError, ScopeResourceNotFoundError) as error:
        raise _not_found(error) from error
    except (ScopeReviewConflictError, ScopeApprovalConflictError) as error:
        raise _conflict(error) from error
    response.headers["Cache-Control"] = "no-store"
    return result
