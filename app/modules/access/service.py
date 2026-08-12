"""Access-link issuance, verification, and revocation."""

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.contracts.actor import ActorContext, ActorKind
from app.contracts.primitives import JobId, ParticipantId, RequestId, utc_now
from app.modules.access.models import ParticipantAccessToken
from app.modules.access.schemas import AccessLinkResponse
from app.modules.completion.models import AuditEventType
from app.modules.completion.service import add_audit_event
from app.modules.move_job.models import JobParticipant

ACCESS_TOKEN_TTL = timedelta(days=7)
ACCESS_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{40,100}$")


class InvalidAccessTokenError(PermissionError):
    """Raised without revealing why a bearer credential failed."""


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


async def issue_access_link(
    session: AsyncSession,
    participant: JobParticipant,
    *,
    actor_participant_id: UUID | None = None,
) -> AccessLinkResponse:
    """Issue a high-entropy credential while storing only its digest."""

    now = utc_now()
    expiry = now + ACCESS_TOKEN_TTL

    secret = secrets.token_urlsafe(32)
    await session.execute(
        select(JobParticipant.id).where(JobParticipant.id == participant.id).with_for_update()
    )
    await session.execute(
        update(ParticipantAccessToken)
        .where(
            ParticipantAccessToken.participant_id == participant.id,
            ParticipantAccessToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    access_link = ParticipantAccessToken(
        participant_id=participant.id,
        token_hash=_hash_secret(secret),
        expires_at=expiry,
    )
    session.add(access_link)
    await session.flush()
    add_audit_event(
        session,
        participant.job_id,
        AuditEventType.ACCESS_LINK_ISSUED,
        actor_participant_id=actor_participant_id,
        payload={
            "access_link_id": str(access_link.id),
            "participant_id": str(participant.id),
            "role": participant.role.value,
        },
    )
    return AccessLinkResponse(
        id=access_link.id,
        job_id=participant.job_id,
        participant_id=participant.id,
        role=participant.role,
        secret=secret,
        expires_at=expiry,
    )


async def load_participant(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
) -> JobParticipant | None:
    statement = select(JobParticipant).where(
        JobParticipant.id == participant_id,
        JobParticipant.job_id == job_id,
    )
    return (await session.scalars(statement)).one_or_none()


async def authenticate_access_token(session: AsyncSession, secret: str) -> ActorContext:
    """Resolve one active bearer secret into the shared actor contract."""

    if ACCESS_SECRET_PATTERN.fullmatch(secret) is None:
        raise InvalidAccessTokenError

    statement = (
        select(ParticipantAccessToken)
        .where(ParticipantAccessToken.token_hash == _hash_secret(secret))
        .options(joinedload(ParticipantAccessToken.participant))
    )
    access_link = (await session.scalars(statement)).one_or_none()
    now = utc_now()
    if (
        access_link is None
        or access_link.revoked_at is not None
        or _as_utc(access_link.expires_at) <= now
    ):
        raise InvalidAccessTokenError

    participant = access_link.participant
    access_link.last_used_at = now
    return ActorContext(
        actor_kind=ActorKind.PARTICIPANT,
        participant_id=ParticipantId(participant.id),
        participant_role=participant.role,
        job_id=JobId(participant.job_id),
        request_id=RequestId(uuid4()),
        trace_id=secrets.token_hex(16),
    )


async def load_access_link(
    session: AsyncSession,
    job_id: UUID,
    access_link_id: UUID,
) -> ParticipantAccessToken | None:
    statement = (
        select(ParticipantAccessToken)
        .join(ParticipantAccessToken.participant)
        .where(
            ParticipantAccessToken.id == access_link_id,
            JobParticipant.job_id == job_id,
        )
        .options(joinedload(ParticipantAccessToken.participant))
    )
    access_link = (await session.scalars(statement)).one_or_none()
    return access_link


async def revoke_access_link(
    session: AsyncSession,
    access_link: ParticipantAccessToken,
    actor_participant_id: UUID,
) -> None:
    if access_link.revoked_at is not None:
        return
    access_link.revoked_at = utc_now()
    add_audit_event(
        session,
        access_link.participant.job_id,
        AuditEventType.ACCESS_LINK_REVOKED,
        actor_participant_id=actor_participant_id,
        payload={
            "access_link_id": str(access_link.id),
            "participant_id": str(access_link.participant.id),
        },
    )
    await session.flush()
