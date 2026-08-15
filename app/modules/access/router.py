"""Access-link rotation and revocation API."""

from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status

from app.api.errors import protected_error_responses
from app.contracts.actor import ParticipantRole
from app.modules.access.auth import (
    BearerSecret,
    CurrentActor,
    InvitationActor,
    authorize_job_actor,
)
from app.modules.access.invitations import (
    ActorAccessLinkNotFoundError,
    InvitationConflictError,
    InvitationNotFoundError,
    InvitationRoleError,
    accept_invitation,
    create_invitation,
    decline_invitation,
    get_actor_self,
    list_invitations,
    reissue_invitation,
    revoke_access_link_tree,
    revoke_invitation,
)
from app.modules.access.schemas import (
    AccessLinkResponse,
    ActorSelfResponse,
    InvitationCreate,
    InvitationIssuedResponse,
    InvitationListResponse,
    InvitationResponse,
)
from app.modules.access.service import (
    InvalidAccessTokenError,
    load_access_link,
    load_participant,
    rotate_access_link,
)
from app.platform.db.dependencies import Session

router = APIRouter(prefix="/move-jobs", tags=["access"])
identity_router = APIRouter(tags=["access"])


@identity_router.get(
    "/me",
    response_model=ActorSelfResponse,
    responses=protected_error_responses(),
    summary="현재 역할 링크와 초대 상태 조회",
)
async def get_actor_self_endpoint(
    response: Response,
    actor: InvitationActor,
    session: Session,
) -> ActorSelfResponse:
    try:
        actor_self = await get_actor_self(session, actor)
    except ActorAccessLinkNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error
    response.headers["Cache-Control"] = "no-store"
    return actor_self


@router.post(
    "/{job_id}/invitations",
    response_model=InvitationIssuedResponse,
    status_code=status.HTTP_201_CREATED,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
    summary="다음 역할 참여자 초대",
)
async def create_invitation_endpoint(
    job_id: UUID,
    command: InvitationCreate,
    response: Response,
    actor: CurrentActor,
    session: Session,
) -> InvitationIssuedResponse:
    authorize_job_actor(
        actor,
        job_id,
        frozenset({ParticipantRole.CUSTOMER, ParticipantRole.COMPANY_MANAGER}),
    )
    try:
        issued = await create_invitation(session, job_id, actor, command)
    except InvitationRoleError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="insufficient role",
        ) from error
    except InvitationConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except InvitationNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="move job not found",
        ) from error
    response.headers["Cache-Control"] = "no-store"
    return issued


@router.get(
    "/{job_id}/invitations",
    response_model=InvitationListResponse,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
    ),
    summary="내 초대 상태 목록 조회",
)
async def list_invitations_endpoint(
    job_id: UUID,
    response: Response,
    actor: CurrentActor,
    session: Session,
) -> InvitationListResponse:
    authorize_job_actor(
        actor,
        job_id,
        frozenset({ParticipantRole.CUSTOMER, ParticipantRole.COMPANY_MANAGER}),
    )
    result = await list_invitations(session, job_id, actor)
    response.headers["Cache-Control"] = "no-store"
    return result


async def _respond_to_invitation(
    *,
    action: str,
    job_id: UUID,
    invitation_id: UUID,
    actor: InvitationActor,
    secret: str,
    session: Session,
) -> InvitationResponse:
    authorize_job_actor(actor, job_id)
    try:
        if action == "accept":
            return await accept_invitation(
                session,
                job_id,
                invitation_id,
                actor,
                secret=secret,
            )
        return await decline_invitation(
            session,
            job_id,
            invitation_id,
            actor,
            secret=secret,
        )
    except InvalidAccessTokenError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error
    except InvitationNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invitation not found",
        ) from error
    except InvitationConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error


@router.post(
    "/{job_id}/invitations/{invitation_id}/accept",
    response_model=InvitationResponse,
    responses=protected_error_responses(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
    summary="초대 수락",
)
async def accept_invitation_endpoint(
    job_id: UUID,
    invitation_id: UUID,
    response: Response,
    actor: InvitationActor,
    secret: BearerSecret,
    session: Session,
) -> InvitationResponse:
    result = await _respond_to_invitation(
        action="accept",
        job_id=job_id,
        invitation_id=invitation_id,
        actor=actor,
        secret=secret,
        session=session,
    )
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/{job_id}/invitations/{invitation_id}/decline",
    response_model=InvitationResponse,
    responses=protected_error_responses(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
    summary="초대 거절",
)
async def decline_invitation_endpoint(
    job_id: UUID,
    invitation_id: UUID,
    response: Response,
    actor: InvitationActor,
    secret: BearerSecret,
    session: Session,
) -> InvitationResponse:
    result = await _respond_to_invitation(
        action="decline",
        job_id=job_id,
        invitation_id=invitation_id,
        actor=actor,
        secret=secret,
        session=session,
    )
    response.headers["Cache-Control"] = "no-store"
    return result


async def _manage_invitation(
    *,
    action: str,
    job_id: UUID,
    invitation_id: UUID,
    actor: CurrentActor,
    session: Session,
) -> InvitationResponse | InvitationIssuedResponse:
    authorize_job_actor(
        actor,
        job_id,
        frozenset({ParticipantRole.CUSTOMER, ParticipantRole.COMPANY_MANAGER}),
    )
    try:
        if action == "reissue":
            return await reissue_invitation(session, job_id, invitation_id, actor)
        return await revoke_invitation(session, job_id, invitation_id, actor)
    except InvitationNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="invitation not found",
        ) from error
    except InvitationConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error


@router.post(
    "/{job_id}/invitations/{invitation_id}/revoke",
    response_model=InvitationResponse,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
    ),
    summary="초대 폐기",
)
async def revoke_invitation_endpoint(
    job_id: UUID,
    invitation_id: UUID,
    response: Response,
    actor: CurrentActor,
    session: Session,
) -> InvitationResponse:
    result = await _manage_invitation(
        action="revoke",
        job_id=job_id,
        invitation_id=invitation_id,
        actor=actor,
        session=session,
    )
    assert isinstance(result, InvitationResponse)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/{job_id}/invitations/{invitation_id}/reissue",
    response_model=InvitationIssuedResponse,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
    summary="초대 링크 재발급",
)
async def reissue_invitation_endpoint(
    job_id: UUID,
    invitation_id: UUID,
    response: Response,
    actor: CurrentActor,
    session: Session,
) -> InvitationIssuedResponse:
    result = await _manage_invitation(
        action="reissue",
        job_id=job_id,
        invitation_id=invitation_id,
        actor=actor,
        session=session,
    )
    assert isinstance(result, InvitationIssuedResponse)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/{job_id}/participants/{participant_id}/access-links",
    response_model=AccessLinkResponse,
    status_code=status.HTTP_201_CREATED,
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
    ),
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
    responses=protected_error_responses(
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
    ),
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
    await revoke_access_link_tree(session, access_link, cast(UUID, actor.participant_id))
