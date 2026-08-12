"""Bearer authentication and job-scoped authorization."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.contracts.actor import ActorContext, ParticipantRole
from app.contracts.primitives import JobId
from app.modules.access.service import InvalidAccessTokenError, authenticate_access_token
from app.platform.db.dependencies import Session

bearer = HTTPBearer(auto_error=False)


async def get_current_actor(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    session: Session,
) -> ActorContext:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid access token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return await authenticate_access_token(session, credentials.credentials)
    except InvalidAccessTokenError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid access token",
            headers={"WWW-Authenticate": "Bearer"},
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
