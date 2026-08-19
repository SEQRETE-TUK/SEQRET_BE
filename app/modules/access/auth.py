"""Bearer authentication and job-scoped authorization."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer

from app.contracts.actor import ActorContext, ParticipantRole
from app.contracts.ports import CachePort
from app.contracts.primitives import JobId
from app.modules.access.service import (
    AccessRateLimitExceededError,
    InvalidAccessTokenError,
    authenticate_access_token,
)
from app.modules.access.workspace import (
    WORKSPACE_SESSION_COOKIE,
    InvalidWorkspaceSessionError,
    authenticate_workspace_actor,
)
from app.platform.db.session import transactional_session
from app.platform.observability import set_correlation_job

bearer = HTTPBearer(auto_error=False)
workspace_cookie = APIKeyCookie(name=WORKSPACE_SESSION_COOKIE, auto_error=False)
WorkspaceCookieSecret = Annotated[str | None, Security(workspace_cookie)]


async def get_bearer_secret(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid access token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials


BearerSecret = Annotated[str, Depends(get_bearer_secret)]


async def _authenticate_request(
    secret: str,
    request: Request,
    *,
    allow_pending_invitation: bool,
) -> ActorContext:
    try:
        settings = request.app.state.runtime_context.settings
        cache: CachePort | None = request.app.state.cache_port
        async with transactional_session(request.app.state.database_session_factory) as session:
            actor = await authenticate_access_token(
                session,
                secret,
                cache=cache,
                logger=request.app.state.observability.logger,
                rate_limit_requests=settings.access_rate_limit_requests,
                rate_limit_window_seconds=settings.access_rate_limit_window_seconds,
                cache_timeout_seconds=settings.cache_timeout_seconds,
                allow_pending_invitation=allow_pending_invitation,
            )
            assert actor.job_id is not None
            set_correlation_job(actor.job_id)
            return actor
    except InvalidAccessTokenError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error
    except AccessRateLimitExceededError as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="access rate limit exceeded",
            headers={"Retry-After": str(error.retry_after_seconds)},
        ) from error


async def get_current_actor(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    cookie_secret: WorkspaceCookieSecret,
    request: Request,
) -> ActorContext:
    if credentials is not None:
        return await _authenticate_request(
            credentials.credentials,
            request,
            allow_pending_invitation=False,
        )
    raw_job_id = request.path_params.get("job_id") or request.headers.get("X-SEQRET-Job-ID")
    if cookie_secret is None or raw_job_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid access token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        job_id = UUID(str(raw_job_id))
        csrf_token = (
            request.headers.get("X-SEQRET-CSRF")
            if request.method not in {"GET", "HEAD", "OPTIONS"}
            else None
        )
        if request.method not in {"GET", "HEAD", "OPTIONS"} and csrf_token is None:
            raise InvalidWorkspaceSessionError
        async with transactional_session(request.app.state.database_session_factory) as session:
            actor = await authenticate_workspace_actor(
                session,
                cookie_secret,
                job_id,
                csrf_token=csrf_token,
            )
        assert actor.job_id is not None
        set_correlation_job(actor.job_id)
        return actor
    except (InvalidWorkspaceSessionError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid workspace session",
        ) from error


CurrentActor = Annotated[ActorContext, Depends(get_current_actor)]


async def get_invitation_actor(
    secret: BearerSecret,
    request: Request,
) -> ActorContext:
    return await _authenticate_request(
        secret,
        request,
        allow_pending_invitation=True,
    )


InvitationActor = Annotated[ActorContext, Depends(get_invitation_actor)]


def authorize_job_actor(
    actor: ActorContext,
    job_id: UUID,
    allowed_roles: frozenset[ParticipantRole] | None = None,
) -> None:
    if not actor.is_participant_for(JobId(job_id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="move job not found")
    if allowed_roles is not None and actor.participant_role not in allowed_roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
