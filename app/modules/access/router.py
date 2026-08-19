"""Access-link rotation and revocation API."""

from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, Response, status

from app.api.errors import protected_error_responses
from app.config import AppEnvironment
from app.contracts.actor import ParticipantRole
from app.modules.access.auth import (
    BearerSecret,
    CurrentActor,
    InvitationActor,
    WorkspaceCookieSecret,
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
from app.modules.access.models import NotificationContactChannel
from app.modules.access.schemas import (
    AccessLinkResponse,
    ActorSelfResponse,
    InvitationCreate,
    InvitationIssuedResponse,
    InvitationListResponse,
    InvitationResponse,
    MoveConnectionCreate,
    WorkspaceContactPointListResponse,
    WorkspaceContactPointResponse,
    WorkspaceContactPointUpsert,
    WorkspaceSessionResponse,
)
from app.modules.access.service import (
    InvalidAccessTokenError,
    load_access_link,
    load_participant,
    rotate_access_link,
)
from app.modules.access.workspace import (
    WORKSPACE_SESSION_COOKIE,
    InvalidMoveConnectionError,
    InvalidWorkspaceSessionError,
    WorkspaceConflictError,
    WorkspaceContactNotFoundError,
    WorkspacePrincipal,
    authenticate_workspace_account,
    create_or_extend_workspace_session,
    delete_contact_point,
    get_workspace_session,
    list_contact_points,
    resolve_move_connection,
    revoke_workspace_session,
    upsert_contact_point,
)
from app.platform.db.dependencies import Session

router = APIRouter(prefix="/move-jobs", tags=["access"])
identity_router = APIRouter(tags=["access"])


def _workspace_cookie_security(request: Request) -> tuple[bool, Literal["lax", "none"]]:
    environment = request.app.state.runtime_context.settings.environment
    deployed = environment in {AppEnvironment.STAGING, AppEnvironment.PRODUCTION}
    return deployed, "none" if deployed else "lax"


def _set_workspace_cookie(request: Request, response: Response, secret: str) -> None:
    secure, same_site = _workspace_cookie_security(request)
    response.set_cookie(
        WORKSPACE_SESSION_COOKIE,
        secret,
        max_age=30 * 24 * 60 * 60,
        path="/api/v1",
        secure=secure,
        httponly=True,
        samesite=same_site,
    )


async def _workspace_principal(
    cookie_secret: str | None,
    session: Session,
    *,
    csrf_token: str | None = None,
) -> WorkspacePrincipal:
    if cookie_secret is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid workspace session"
        )
    try:
        return await authenticate_workspace_account(
            session,
            cookie_secret,
            csrf_token=csrf_token,
        )
    except InvalidWorkspaceSessionError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid workspace session",
        ) from error


@identity_router.post(
    "/sessions",
    response_model=WorkspaceSessionResponse,
    status_code=status.HTTP_201_CREATED,
    responses=protected_error_responses(status.HTTP_409_CONFLICT),
    summary="검증된 역할 링크를 안전한 작업공간 세션에 연결",
)
async def create_workspace_session_endpoint(
    request: Request,
    response: Response,
    actor: CurrentActor,
    session: Session,
    cookie_secret: WorkspaceCookieSecret,
) -> WorkspaceSessionResponse:
    try:
        issued = await create_or_extend_workspace_session(
            session,
            cast(UUID, actor.participant_id),
            current_cookie_secret=cookie_secret,
        )
    except WorkspaceConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    if issued.cookie_secret is not None:
        _set_workspace_cookie(request, response, issued.cookie_secret)
    response.headers["Cache-Control"] = "no-store"
    return issued.response


@identity_router.post(
    "/connections",
    response_model=WorkspaceSessionResponse,
    status_code=status.HTTP_201_CREATED,
    responses=protected_error_responses(status.HTTP_409_CONFLICT),
    summary="공용 이사 연결 코드와 선택 역할로 작업공간 연결",
)
async def create_move_connection_endpoint(
    command: MoveConnectionCreate,
    request: Request,
    response: Response,
    session: Session,
    cookie_secret: WorkspaceCookieSecret,
) -> WorkspaceSessionResponse:
    try:
        participant = await resolve_move_connection(
            session,
            command.connection_code,
            command.role,
        )
        issued = await create_or_extend_workspace_session(
            session,
            participant.id,
            current_cookie_secret=cookie_secret,
        )
    except InvalidMoveConnectionError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid move connection code",
        ) from error
    except WorkspaceConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    if issued.cookie_secret is not None:
        _set_workspace_cookie(request, response, issued.cookie_secret)
    response.headers["Cache-Control"] = "no-store"
    return issued.response


@identity_router.get(
    "/session",
    response_model=WorkspaceSessionResponse,
    responses=protected_error_responses(),
    summary="새로고침 후 작업공간 세션 복원",
)
async def get_workspace_session_endpoint(
    request: Request,
    response: Response,
    session: Session,
    cookie_secret: WorkspaceCookieSecret,
) -> WorkspaceSessionResponse:
    if cookie_secret is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid workspace session"
        )
    try:
        result = await get_workspace_session(session, cookie_secret)
    except InvalidWorkspaceSessionError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid workspace session",
        ) from error
    response.headers["Cache-Control"] = "no-store"
    return result


@identity_router.delete(
    "/session",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=protected_error_responses(),
    summary="현재 작업공간 세션 종료",
)
async def delete_workspace_session_endpoint(
    request: Request,
    response: Response,
    session: Session,
    cookie_secret: WorkspaceCookieSecret,
    csrf_token: str = Header(alias="X-SEQRET-CSRF"),
) -> None:
    await _workspace_principal(cookie_secret, session, csrf_token=csrf_token)
    assert cookie_secret is not None
    await revoke_workspace_session(session, cookie_secret)
    secure, same_site = _workspace_cookie_security(request)
    response.delete_cookie(
        WORKSPACE_SESSION_COOKIE,
        path="/api/v1",
        secure=secure,
        httponly=True,
        samesite=same_site,
    )


@identity_router.get(
    "/session/contact-points",
    response_model=WorkspaceContactPointListResponse,
    responses=protected_error_responses(),
    summary="외부 알림 연락처 목록 조회",
)
async def list_workspace_contact_points_endpoint(
    request: Request,
    response: Response,
    session: Session,
    cookie_secret: WorkspaceCookieSecret,
) -> WorkspaceContactPointListResponse:
    principal = await _workspace_principal(cookie_secret, session)
    response.headers["Cache-Control"] = "no-store"
    return await list_contact_points(session, principal.account_id)


@identity_router.put(
    "/session/contact-points/{channel}",
    response_model=WorkspaceContactPointResponse,
    responses=protected_error_responses(),
    summary="외부 알림 연락처와 명시적 수신 동의 저장",
)
async def upsert_workspace_contact_point_endpoint(
    channel: NotificationContactChannel,
    command: WorkspaceContactPointUpsert,
    request: Request,
    response: Response,
    session: Session,
    cookie_secret: WorkspaceCookieSecret,
    csrf_token: str = Header(alias="X-SEQRET-CSRF"),
) -> WorkspaceContactPointResponse:
    principal = await _workspace_principal(cookie_secret, session, csrf_token=csrf_token)
    try:
        result = await upsert_contact_point(session, principal.account_id, channel, command)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error
    response.headers["Cache-Control"] = "no-store"
    return result


@identity_router.delete(
    "/session/contact-points/{channel}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=protected_error_responses(status.HTTP_404_NOT_FOUND),
    summary="외부 알림 연락처 삭제",
)
async def delete_workspace_contact_point_endpoint(
    channel: NotificationContactChannel,
    request: Request,
    session: Session,
    cookie_secret: WorkspaceCookieSecret,
    csrf_token: str = Header(alias="X-SEQRET-CSRF"),
) -> None:
    principal = await _workspace_principal(cookie_secret, session, csrf_token=csrf_token)
    try:
        await delete_contact_point(session, principal.account_id, channel)
    except WorkspaceContactNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="contact point not found",
        ) from error


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
