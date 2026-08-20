"""Authenticated customer AI-draft review API."""

from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status

from app.api.errors import protected_error_responses
from app.contracts.actor import ParticipantRole
from app.contracts.ports import ProviderError
from app.modules.access.auth import CurrentActor, authorize_job_actor
from app.modules.analysis_review.schemas import (
    AnalysisReviewComplete,
    AnalysisReviewResponse,
)
from app.modules.analysis_review.service import (
    AnalysisReviewConflictError,
    AnalysisReviewNotFoundError,
    complete_analysis_review,
    get_analysis_review,
)
from app.modules.capture.router import Storage, storage_error
from app.platform.db.dependencies import Session

router = APIRouter(prefix="/move-jobs", tags=["analysis-review"])

CUSTOMER_ROLE = frozenset({ParticipantRole.CUSTOMER})


def _review_error(error: Exception) -> HTTPException:
    if isinstance(error, AnalysisReviewNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="analysis review not found",
        )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="analysis review is not ready or is no longer current",
    )


@router.get(
    "/{job_id}/analysis-review",
    response_model=AnalysisReviewResponse,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ),
    summary="AI 작업범위 초안 검토 조회",
)
async def get_analysis_review_endpoint(
    job_id: UUID,
    response: Response,
    actor: CurrentActor,
    session: Session,
    storage: Storage,
) -> AnalysisReviewResponse:
    authorize_job_actor(actor, job_id, CUSTOMER_ROLE)
    try:
        result = await get_analysis_review(
            session,
            job_id,
            cast(UUID, actor.participant_id),
            storage=storage,
        )
    except (AnalysisReviewNotFoundError, AnalysisReviewConflictError) as error:
        raise _review_error(error) from error
    except ProviderError as error:
        raise storage_error(error) from error
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/{job_id}/analysis-review/complete",
    response_model=AnalysisReviewResponse,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
    summary="AI 작업범위 초안 검토 완료",
)
async def complete_analysis_review_endpoint(
    job_id: UUID,
    command: AnalysisReviewComplete,
    response: Response,
    actor: CurrentActor,
    session: Session,
) -> AnalysisReviewResponse:
    authorize_job_actor(actor, job_id, CUSTOMER_ROLE)
    try:
        return await complete_analysis_review(
            session,
            job_id,
            cast(UUID, actor.participant_id),
            command,
        )
    except (AnalysisReviewNotFoundError, AnalysisReviewConflictError) as error:
        raise _review_error(error) from error
    finally:
        response.headers["Cache-Control"] = "no-store"
