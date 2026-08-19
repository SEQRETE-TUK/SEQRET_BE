"""Frontend-facing field issue and change proposal routes."""

from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status

from app.api.errors import protected_error_responses
from app.contracts.actor import ParticipantRole
from app.contracts.ports import ProviderError
from app.modules.access.auth import CurrentActor, authorize_job_actor
from app.modules.capture.router import Storage, storage_error
from app.modules.field_change.schemas import (
    ChangeProposalCreate,
    ChangeProposalDecisionCreate,
    ChangeProposalDecisionResponse,
    ChangeProposalExplanationCreate,
    ChangeProposalResponse,
    ChangeProposalView,
    FieldIssueCreate,
    FieldIssueEvidenceReadResponse,
    FieldIssueResponse,
)
from app.modules.field_change.service import (
    FieldChangeConflictError,
    FieldChangeInvalidError,
    FieldChangeNotFoundError,
    create_change_proposal,
    create_field_issue,
    create_field_issue_evidence_read_url,
    decide_change_proposal,
    explain_change_proposal,
    get_change_proposal,
    list_field_issues,
)
from app.modules.scope.service import (
    ChangeRequestConflictError,
    ScopeApprovalConflictError,
    ScopeResourceNotFoundError,
    ScopeVersionConflictError,
)
from app.platform.db.dependencies import Session

router = APIRouter(prefix="/move-jobs", tags=["field-change"])
PROPOSAL_VIEW_ROLES = frozenset({ParticipantRole.CUSTOMER, ParticipantRole.COMPANY_MANAGER})
ISSUE_VIEW_ROLES = frozenset(
    {
        ParticipantRole.CUSTOMER,
        ParticipantRole.COMPANY_MANAGER,
        ParticipantRole.FIELD_WORKER,
    }
)
ISSUE_REPORT_ROLES = frozenset({ParticipantRole.COMPANY_MANAGER, ParticipantRole.FIELD_WORKER})
ISSUE_EVIDENCE_VIEW_ROLES = frozenset(
    {ParticipantRole.COMPANY_MANAGER, ParticipantRole.FIELD_WORKER}
)


def _not_found(error: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="field change resource not found",
    )


def _conflict(error: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="field change state conflict",
    )


@router.post(
    "/{job_id}/field-issues",
    response_model=FieldIssueResponse,
    status_code=status.HTTP_201_CREATED,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
    summary="업체 또는 현장기사 이슈와 증거 보고",
)
async def create_field_issue_endpoint(
    job_id: UUID,
    command: FieldIssueCreate,
    actor: CurrentActor,
    session: Session,
) -> FieldIssueResponse:
    authorize_job_actor(actor, job_id, ISSUE_REPORT_ROLES)
    try:
        return await create_field_issue(
            session,
            job_id,
            cast(UUID, actor.participant_id),
            command,
        )
    except FieldChangeNotFoundError as error:
        raise _not_found(error) from error
    except (FieldChangeConflictError, FieldChangeInvalidError) as error:
        raise _conflict(error) from error


@router.get(
    "/{job_id}/field-issues",
    response_model=tuple[FieldIssueResponse, ...],
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
    ),
    summary="처리할 현장 이슈 목록 조회",
)
async def list_field_issues_endpoint(
    job_id: UUID,
    actor: CurrentActor,
    session: Session,
) -> tuple[FieldIssueResponse, ...]:
    authorize_job_actor(actor, job_id, ISSUE_VIEW_ROLES)
    return await list_field_issues(session, job_id)


@router.get(
    "/{job_id}/field-issues/{field_issue_id}/evidence/{media_asset_id}/read-url",
    response_model=FieldIssueEvidenceReadResponse,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ),
    summary="업체·현장기사 현장 이슈 증거 열람 URL 발급",
)
async def create_field_issue_evidence_read_url_endpoint(
    job_id: UUID,
    field_issue_id: UUID,
    media_asset_id: UUID,
    response: Response,
    actor: CurrentActor,
    session: Session,
    storage: Storage,
) -> FieldIssueEvidenceReadResponse:
    authorize_job_actor(actor, job_id, ISSUE_EVIDENCE_VIEW_ROLES)
    try:
        result = await create_field_issue_evidence_read_url(
            session,
            storage,
            job_id,
            field_issue_id,
            media_asset_id,
            cast(UUID, actor.participant_id),
            cast(ParticipantRole, actor.participant_role),
        )
    except FieldChangeNotFoundError as error:
        raise _not_found(error) from error
    except FieldChangeConflictError as error:
        raise _conflict(error) from error
    except ProviderError as error:
        raise storage_error(error) from error
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/{job_id}/change-proposals",
    response_model=ChangeProposalResponse,
    status_code=status.HTTP_201_CREATED,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
    summary="업체 현장 변경안과 금액 제안",
)
async def create_change_proposal_endpoint(
    job_id: UUID,
    command: ChangeProposalCreate,
    response: Response,
    actor: CurrentActor,
    session: Session,
) -> ChangeProposalResponse:
    authorize_job_actor(actor, job_id, frozenset({ParticipantRole.COMPANY_MANAGER}))
    try:
        result = await create_change_proposal(
            session,
            job_id,
            cast(UUID, actor.participant_id),
            command,
            trace_id=actor.trace_id,
        )
    except FieldChangeNotFoundError as error:
        raise _not_found(error) from error
    except (FieldChangeConflictError, FieldChangeInvalidError) as error:
        raise _conflict(error) from error
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get(
    "/{job_id}/change-proposals/{proposal_id}",
    response_model=ChangeProposalView,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ),
    summary="고객 현장 변경 결정 화면 조회",
)
async def get_change_proposal_endpoint(
    job_id: UUID,
    proposal_id: UUID,
    response: Response,
    actor: CurrentActor,
    session: Session,
    storage: Storage,
) -> ChangeProposalView:
    authorize_job_actor(actor, job_id, PROPOSAL_VIEW_ROLES)
    try:
        view = await get_change_proposal(
            session,
            storage,
            job_id,
            proposal_id,
            cast(UUID, actor.participant_id),
            cast(ParticipantRole, actor.participant_role),
        )
    except FieldChangeNotFoundError as error:
        raise _not_found(error) from error
    except FieldChangeConflictError as error:
        raise _conflict(error) from error
    except ProviderError as error:
        raise storage_error(error) from error
    response.headers["Cache-Control"] = "no-store"
    return view


@router.post(
    "/{job_id}/change-proposals/{proposal_id}/decision",
    response_model=ChangeProposalDecisionResponse,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
    summary="고객 현장 변경 승인·거절·설명 요청",
)
async def decide_change_proposal_endpoint(
    job_id: UUID,
    proposal_id: UUID,
    command: ChangeProposalDecisionCreate,
    actor: CurrentActor,
    session: Session,
) -> ChangeProposalDecisionResponse:
    authorize_job_actor(actor, job_id, frozenset({ParticipantRole.CUSTOMER}))
    try:
        return await decide_change_proposal(
            session,
            job_id,
            proposal_id,
            cast(UUID, actor.participant_id),
            command,
            trace_id=actor.trace_id,
        )
    except FieldChangeNotFoundError as error:
        raise _not_found(error) from error
    except (
        FieldChangeConflictError,
        ChangeRequestConflictError,
        ScopeApprovalConflictError,
        ScopeResourceNotFoundError,
        ScopeVersionConflictError,
    ) as error:
        raise _conflict(error) from error


@router.post(
    "/{job_id}/change-proposals/{proposal_id}/explanation",
    response_model=ChangeProposalResponse,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
    summary="업체 현장 변경 설명 제출",
)
async def explain_change_proposal_endpoint(
    job_id: UUID,
    proposal_id: UUID,
    command: ChangeProposalExplanationCreate,
    actor: CurrentActor,
    session: Session,
) -> ChangeProposalResponse:
    authorize_job_actor(actor, job_id, frozenset({ParticipantRole.COMPANY_MANAGER}))
    try:
        return await explain_change_proposal(
            session,
            job_id,
            proposal_id,
            cast(UUID, actor.participant_id),
            command.explanation,
        )
    except FieldChangeNotFoundError as error:
        raise _not_found(error) from error
    except FieldChangeConflictError as error:
        raise _conflict(error) from error
