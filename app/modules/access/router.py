"""Access-link rotation and revocation API."""

from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status

from app.contracts.actor import ParticipantRole
from app.modules.access.auth import BearerSecret, CurrentActor, authorize_job_actor
from app.modules.access.schemas import AccessLinkResponse
from app.modules.access.service import (
    InvalidAccessTokenError,
    load_access_link,
    load_participant,
    revoke_access_link,
    rotate_access_link,
)
from app.platform.db.dependencies import Session

router = APIRouter(prefix="/move-jobs", tags=["access"])


@router.post(
    "/{job_id}/participants/{participant_id}/access-links",
    response_model=AccessLinkResponse,
    status_code=status.HTTP_201_CREATED,
    summary="자기 역할 링크 회전",
)
async def create_access_link_endpoint(
    job_id: UUID,
    participant_id: UUID,
    response: Response,
    actor: CurrentActor,
    secret: BearerSecret,
    session: Session,
) -> AccessLinkResponse:
    authorize_job_actor(actor, job_id)
    if actor.participant_id != participant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
    participant = await load_participant(session, job_id, participant_id)
    if participant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="participant not found")
    try:
        access_link = await rotate_access_link(
            session,
            participant,
            current_secret=secret,
            actor_participant_id=cast(UUID, actor.participant_id),
        )
    except InvalidAccessTokenError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error
    response.headers["Cache-Control"] = "no-store"
    return access_link


@router.post(
    "/{job_id}/access-links/{access_link_id}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="참여자 역할 링크 철회",
)
async def revoke_access_link_endpoint(
    job_id: UUID,
    access_link_id: UUID,
    actor: CurrentActor,
    session: Session,
) -> None:
    authorize_job_actor(actor, job_id)
    access_link = await load_access_link(session, job_id, access_link_id)
    if access_link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="access link not found")
    if (
        actor.participant_role is not ParticipantRole.COMPANY_MANAGER
        and actor.participant_id != access_link.participant.id
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
    await revoke_access_link(session, access_link, cast(UUID, actor.participant_id))
