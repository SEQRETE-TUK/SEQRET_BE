"""Bearer authentication and job-scoped authorization."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.contracts.actor import ActorContext, ParticipantRole
from app.contracts.ports import CachePort
from app.contracts.primitives import JobId
from app.modules.access.service import (
    AccessRateLimitExceededError,
    InvalidAccessTokenError,
    authenticate_access_token,
)
from app.platform.db.session import transactional_session
from app.platform.observability import set_correlation_job

bearer = HTTPBearer(auto_error=False)


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


async def get_current_actor(
    secret: BearerSecret,
    request: Request,
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


CurrentActor = Annotated[ActorContext, Depends(get_current_actor)]


def authorize_job_actor(
    actor: ActorContext,
    job_id: UUID,
    allowed_roles: frozenset[ParticipantRole] | None = None,
) -> None:
    if not actor.is_participant_for(JobId(job_id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="move job not found")
    if allowed_roles is not None and actor.participant_role not in allowed_roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
