"""Access-link issuance, verification, and revocation."""

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.contracts.actor import ActorContext, ActorKind
from app.contracts.ports import CachePort, ProviderError, ProviderErrorKind
from app.contracts.primitives import JobId, ParticipantId, RequestId, utc_now
from app.modules.access.models import ParticipantAccessToken
from app.modules.access.schemas import AccessLinkResponse
from app.modules.completion.models import AuditEventType
from app.modules.completion.service import add_audit_event
from app.modules.move_job.models import JobParticipant
from app.platform.observability import current_correlation

ACCESS_TOKEN_TTL = timedelta(days=7)
ACCESS_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{40,100}$")


class InvalidAccessTokenError(PermissionError):
    """Raised without revealing why a bearer credential failed."""


class AccessRateLimitExceededError(PermissionError):
    """Raised after a valid access link exceeds its fixed request window."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("access rate limit exceeded")
        self.retry_after_seconds = retry_after_seconds


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


async def _increment_database_rate_window(
    session: AsyncSession,
    access_link_id: UUID,
    *,
    now: datetime,
    window_seconds: int,
) -> int:
    """Atomically increment the recoverable counter stored with one access link."""

    window_cutoff = now - timedelta(seconds=window_seconds)
    window_expired = (ParticipantAccessToken.rate_window_started_at.is_(None)) | (
        ParticipantAccessToken.rate_window_started_at <= window_cutoff
    )
    statement = (
        update(ParticipantAccessToken)
        .where(
            ParticipantAccessToken.id == access_link_id,
            ParticipantAccessToken.revoked_at.is_(None),
            ParticipantAccessToken.expires_at > now,
        )
        .values(
            rate_window_started_at=case(
                (window_expired, now),
                else_=ParticipantAccessToken.rate_window_started_at,
            ),
            rate_window_count=case(
                (window_expired, 1),
                else_=ParticipantAccessToken.rate_window_count + 1,
            ),
        )
        .returning(ParticipantAccessToken.rate_window_count)
        .execution_options(synchronize_session=False)
    )
    count = (await session.execute(statement)).scalar_one_or_none()
    if count is None:
        raise InvalidAccessTokenError
    return count


async def _enforce_access_rate_limit(
    session: AsyncSession,
    access_link: ParticipantAccessToken,
    cache: CachePort | None,
    *,
    now: datetime,
    request_limit: int,
    window_seconds: int,
    timeout_seconds: float,
) -> None:
    count: int | None = None
    if cache is not None:
        try:
            count = await cache.increment_fixed_window(
                key=f"seqret:rate:access:{access_link.token_hash}",
                window_seconds=window_seconds,
                timeout_seconds=timeout_seconds,
            )
        except ProviderError as error:
            if error.kind not in {
                ProviderErrorKind.DEADLINE_EXCEEDED,
                ProviderErrorKind.UNAVAILABLE,
            }:
                raise
            count = None
    if count is None:
        count = await _increment_database_rate_window(
            session,
            access_link.id,
            now=now,
            window_seconds=window_seconds,
        )
    if count > request_limit:
        raise AccessRateLimitExceededError(window_seconds)


async def authenticate_access_token(
    session: AsyncSession,
    secret: str,
    *,
    cache: CachePort | None,
    rate_limit_requests: int,
    rate_limit_window_seconds: int,
    cache_timeout_seconds: float,
) -> ActorContext:
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
    await _enforce_access_rate_limit(
        session,
        access_link,
        cache,
        now=now,
        request_limit=rate_limit_requests,
        window_seconds=rate_limit_window_seconds,
        timeout_seconds=cache_timeout_seconds,
    )
    access_link.last_used_at = now
    correlation = current_correlation()
    return ActorContext(
        actor_kind=ActorKind.PARTICIPANT,
        participant_id=ParticipantId(participant.id),
        participant_role=participant.role,
        job_id=JobId(participant.job_id),
        request_id=(correlation.request_id if correlation is not None else RequestId(uuid4())),
        trace_id=(correlation.trace_id if correlation is not None else secrets.token_hex(16)),
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
