"""Durable workspace sessions and consent-backed external contact points."""

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import String, func, select, update
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.contracts.actor import ActorContext, ActorKind, ParticipantRole
from app.contracts.primitives import JobId, ParticipantId, RequestId, utc_now
from app.modules.access.models import (
    InvitationStatus,
    NotificationContactChannel,
    ParticipantInvitation,
    WorkspaceAccount,
    WorkspaceContactPoint,
    WorkspaceMembership,
    WorkspaceSession,
)
from app.modules.access.schemas import (
    InvitationResponse,
    MoveConnectionPreviewResponse,
    WorkspaceContactPointListResponse,
    WorkspaceContactPointResponse,
    WorkspaceContactPointUpsert,
    WorkspaceMemberResponse,
    WorkspaceSessionResponse,
)
from app.modules.move_job.models import JobParticipant, MoveJob, MoveJobStatus
from app.modules.notification.models import (
    NotificationChannel,
    NotificationDelivery,
    NotificationStatus,
)
from app.platform.observability import current_correlation

WORKSPACE_SESSION_COOKIE = "seqret_workspace_session"
WORKSPACE_SESSION_TTL = timedelta(days=30)
WORKSPACE_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{40,100}$")
PHONE_PATTERN = re.compile(r"^\+[1-9][0-9]{7,14}$")
REVOKED_CONTACT_DESTINATION_PREFIX = "revoked:"
DEFAULT_ROLE_NAMES = {
    ParticipantRole.CUSTOMER: "고객",
    ParticipantRole.COMPANY_MANAGER: "이사업체",
    ParticipantRole.FIELD_WORKER: "현장기사",
}


class InvalidWorkspaceSessionError(PermissionError):
    """Raised without exposing whether a cookie, account, or membership failed."""


class WorkspaceConflictError(ValueError):
    """Raised when one workspace tries to claim an incompatible participant."""


class WorkspaceContactNotFoundError(LookupError):
    """Raised when a requested contact point does not exist."""


class InvalidMoveConnectionError(PermissionError):
    """Raised without exposing whether a shared move code or role was missing."""


@dataclass(frozen=True, slots=True)
class WorkspacePrincipal:
    session_id: UUID
    account_id: UUID
    role: ParticipantRole
    display_name: str
    expires_at: datetime
    csrf_token: str


@dataclass(frozen=True, slots=True)
class IssuedWorkspaceSession:
    response: WorkspaceSessionResponse
    cookie_secret: str | None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _invitation_response(invitation: ParticipantInvitation) -> InvitationResponse:
    return InvitationResponse(
        id=invitation.id,
        job_id=invitation.job_id,
        issuer_participant_id=invitation.issuer_participant_id,
        invitee_participant_id=invitation.invitee_participant_id,
        role=invitation.role,
        display_name=invitation.invitee.display_name,
        status=invitation.status,
        issued_at=_as_utc(invitation.issued_at),
        expires_at=_as_utc(invitation.expires_at),
        resolved_at=(
            _as_utc(invitation.resolved_at) if invitation.resolved_at is not None else None
        ),
    )


async def _load_session(
    session: AsyncSession,
    secret: str,
    *,
    touch: bool,
) -> tuple[WorkspaceSession, WorkspaceAccount]:
    if WORKSPACE_SECRET_PATTERN.fullmatch(secret) is None:
        raise InvalidWorkspaceSessionError
    row = (
        await session.execute(
            select(WorkspaceSession, WorkspaceAccount)
            .join(WorkspaceAccount, WorkspaceAccount.id == WorkspaceSession.account_id)
            .where(WorkspaceSession.token_hash == _hash_secret(secret))
        )
    ).one_or_none()
    now = utc_now()
    if row is None:
        raise InvalidWorkspaceSessionError
    workspace_session, account = row
    if workspace_session.revoked_at is not None or _as_utc(workspace_session.expires_at) <= now:
        raise InvalidWorkspaceSessionError
    if touch:
        workspace_session.last_used_at = now
        await session.flush()
    return workspace_session, account


async def authenticate_workspace_account(
    session: AsyncSession,
    secret: str,
    *,
    csrf_token: str | None = None,
) -> WorkspacePrincipal:
    """Authenticate the opaque HttpOnly cookie and optionally require its CSRF token."""

    workspace_session, account = await _load_session(session, secret, touch=True)
    if csrf_token is not None and not secrets.compare_digest(
        workspace_session.csrf_token,
        csrf_token,
    ):
        raise InvalidWorkspaceSessionError
    return WorkspacePrincipal(
        session_id=workspace_session.id,
        account_id=account.id,
        role=account.role,
        display_name=account.display_name,
        expires_at=_as_utc(workspace_session.expires_at),
        csrf_token=workspace_session.csrf_token,
    )


async def authenticate_workspace_actor(
    session: AsyncSession,
    secret: str,
    job_id: UUID,
    *,
    csrf_token: str | None = None,
) -> ActorContext:
    """Resolve one active account membership into the existing job-scoped actor contract."""

    principal = await authenticate_workspace_account(
        session,
        secret,
        csrf_token=csrf_token,
    )
    participant = await session.scalar(
        select(JobParticipant)
        .join(
            WorkspaceMembership,
            WorkspaceMembership.participant_id == JobParticipant.id,
        )
        .where(
            WorkspaceMembership.account_id == principal.account_id,
            WorkspaceMembership.revoked_at.is_(None),
            JobParticipant.job_id == job_id,
            JobParticipant.role == principal.role,
        )
    )
    if participant is None:
        raise InvalidWorkspaceSessionError
    invitation = await session.scalar(
        select(ParticipantInvitation).where(
            ParticipantInvitation.invitee_participant_id == participant.id
        )
    )
    if invitation is not None and invitation.status is not InvitationStatus.ACCEPTED:
        raise InvalidWorkspaceSessionError
    correlation = current_correlation()
    return ActorContext(
        actor_kind=ActorKind.PARTICIPANT,
        participant_id=ParticipantId(participant.id),
        participant_role=participant.role,
        job_id=JobId(participant.job_id),
        request_id=(correlation.request_id if correlation is not None else RequestId(uuid4())),
        trace_id=(correlation.trace_id if correlation is not None else secrets.token_hex(16)),
    )


async def _workspace_response(
    session: AsyncSession,
    workspace_session: WorkspaceSession,
    account: WorkspaceAccount,
) -> WorkspaceSessionResponse:
    rows = (
        await session.execute(
            select(WorkspaceMembership, JobParticipant)
            .join(JobParticipant, JobParticipant.id == WorkspaceMembership.participant_id)
            .where(
                WorkspaceMembership.account_id == account.id,
                WorkspaceMembership.revoked_at.is_(None),
            )
            .order_by(WorkspaceMembership.joined_at, WorkspaceMembership.id)
        )
    ).all()
    participant_ids = tuple(participant.id for _, participant in rows)
    invitation_by_participant: dict[UUID, ParticipantInvitation] = {}
    if participant_ids:
        invitations = (
            await session.scalars(
                select(ParticipantInvitation)
                .where(ParticipantInvitation.invitee_participant_id.in_(participant_ids))
                .options(joinedload(ParticipantInvitation.invitee))
            )
        ).all()
        invitation_by_participant = {
            invitation.invitee_participant_id: invitation for invitation in invitations
        }
    return WorkspaceSessionResponse(
        account_id=account.id,
        role=account.role,
        display_name=account.display_name,
        expires_at=_as_utc(workspace_session.expires_at),
        csrf_token=workspace_session.csrf_token,
        members=tuple(
            WorkspaceMemberResponse(
                job_id=participant.job_id,
                participant_id=participant.id,
                role=participant.role,
                display_name=participant.display_name,
                invitation=(
                    _invitation_response(invitation_by_participant[participant.id])
                    if participant.id in invitation_by_participant
                    else None
                ),
            )
            for _, participant in rows
        ),
    )


async def create_or_extend_workspace_session(
    session: AsyncSession,
    participant_id: UUID,
    *,
    current_cookie_secret: str | None,
) -> IssuedWorkspaceSession:
    """Create a workspace or attach another same-role accepted participant to it."""

    participant = await session.scalar(
        select(JobParticipant).where(JobParticipant.id == participant_id).with_for_update()
    )
    if participant is None:
        raise InvalidWorkspaceSessionError
    invitation = await session.scalar(
        select(ParticipantInvitation).where(
            ParticipantInvitation.invitee_participant_id == participant.id
        )
    )
    if invitation is not None and invitation.status is not InvitationStatus.ACCEPTED:
        raise WorkspaceConflictError("invitation must be accepted before creating a session")

    workspace_session: WorkspaceSession | None = None
    account: WorkspaceAccount | None = None
    if current_cookie_secret is not None:
        try:
            workspace_session, account = await _load_session(
                session,
                current_cookie_secret,
                touch=True,
            )
        except InvalidWorkspaceSessionError:
            workspace_session = account = None

    membership = await session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.participant_id == participant.id,
            WorkspaceMembership.revoked_at.is_(None),
        )
    )
    if membership is not None:
        membership_account = await session.get(WorkspaceAccount, membership.account_id)
        assert membership_account is not None
        if account is None:
            # One job capability must never recover every other job previously grouped in
            # the same browser workspace. Detach only the proven participant and require
            # the remaining capabilities to be presented again for a new browser session.
            membership.revoked_at = utc_now()
            await session.execute(
                update(NotificationDelivery)
                .where(
                    NotificationDelivery.recipient_participant_id == participant.id,
                    NotificationDelivery.channel != NotificationChannel.IN_APP,
                    NotificationDelivery.status == NotificationStatus.PENDING,
                )
                .values(
                    status=NotificationStatus.FAILED,
                    last_error_code="workspace_relinked",
                    next_attempt_at=None,
                    lock_token=None,
                    locked_until=None,
                )
            )
            await session.flush()
            membership = None
        elif account.id != membership_account.id:
            raise WorkspaceConflictError("participant belongs to another workspace")
        else:
            account = membership_account
    if membership is None:
        if account is not None:
            if account.role is not participant.role:
                raise WorkspaceConflictError("workspace role does not match participant role")
        else:
            account = WorkspaceAccount(
                role=participant.role,
                display_name=participant.display_name,
            )
            session.add(account)
            await session.flush()
        session.add(
            WorkspaceMembership(
                account_id=account.id,
                participant_id=participant.id,
                joined_at=utc_now(),
            )
        )

    account = cast(WorkspaceAccount, account)
    cookie_secret: str | None = None
    if workspace_session is None:
        now = utc_now()
        cookie_secret = secrets.token_urlsafe(32)
        workspace_session = WorkspaceSession(
            account_id=account.id,
            token_hash=_hash_secret(cookie_secret),
            csrf_token=secrets.token_urlsafe(32),
            expires_at=now + WORKSPACE_SESSION_TTL,
            last_used_at=now,
            created_at=now,
        )
        session.add(workspace_session)
    await session.flush()
    return IssuedWorkspaceSession(
        response=await _workspace_response(session, workspace_session, account),
        cookie_secret=cookie_secret,
    )


async def _load_move_connection_job(
    session: AsyncSession,
    connection_code: str,
) -> MoveJob:
    # ponytail: 32-bit display codes can collide; use a stored random code if this leaves demo use.
    prefix = connection_code.removeprefix("MOVE-").lower()
    jobs = tuple(
        (
            await session.scalars(
                select(MoveJob)
                .where(
                    MoveJob.status != MoveJobStatus.CANCELED,
                    func.lower(sql_cast(MoveJob.id, String)).like(f"{prefix}%"),
                )
                .limit(2)
            )
        ).all()
    )
    if len(jobs) != 1:
        raise InvalidMoveConnectionError
    return jobs[0]


async def preview_move_connection(
    session: AsyncSession,
    connection_code: str,
    role: ParticipantRole,
) -> MoveConnectionPreviewResponse:
    """Resolve the display identity without creating participants or sessions."""

    job = await _load_move_connection_job(session, connection_code)
    display_name = await session.scalar(
        select(JobParticipant.display_name).where(
            JobParticipant.job_id == job.id,
            JobParticipant.role == role,
        )
    )
    return MoveConnectionPreviewResponse(
        role=role,
        display_name=display_name or DEFAULT_ROLE_NAMES[role],
    )


async def resolve_move_connection(
    session: AsyncSession,
    connection_code: str,
    role: ParticipantRole,
) -> JobParticipant:
    """Resolve a displayable move code and materialize the selected demo role."""

    job = await _load_move_connection_job(session, connection_code)
    participant = await session.scalar(
        select(JobParticipant).where(
            JobParticipant.job_id == job.id,
            JobParticipant.role == role,
        )
    )
    if participant is None:
        participant = JobParticipant(
            job_id=job.id,
            role=role,
            display_name=DEFAULT_ROLE_NAMES[role],
        )
        session.add(participant)
        await session.flush()
    invitation = await session.scalar(
        select(ParticipantInvitation).where(
            ParticipantInvitation.invitee_participant_id == participant.id
        )
    )
    if invitation is not None and invitation.status is not InvitationStatus.ACCEPTED:
        invitation.status = InvitationStatus.ACCEPTED
        invitation.resolved_at = utc_now()
        await session.flush()
    return participant


async def get_workspace_session(
    session: AsyncSession,
    secret: str,
) -> WorkspaceSessionResponse:
    workspace_session, account = await _load_session(session, secret, touch=True)
    return await _workspace_response(session, workspace_session, account)


async def revoke_workspace_session(session: AsyncSession, secret: str) -> None:
    workspace_session, _ = await _load_session(session, secret, touch=False)
    workspace_session.revoked_at = utc_now()
    await session.flush()


async def revoke_workspace_memberships(
    session: AsyncSession,
    participant_id: UUID,
) -> None:
    await session.execute(
        update(WorkspaceMembership)
        .where(
            WorkspaceMembership.participant_id == participant_id,
            WorkspaceMembership.revoked_at.is_(None),
        )
        .values(revoked_at=utc_now())
    )


def _normalized_destination(channel: NotificationContactChannel, destination: str) -> str:
    normalized = destination.strip()
    if channel is NotificationContactChannel.EMAIL:
        normalized = normalized.lower()
        if normalized.count("@") != 1:
            raise ValueError("email destination is invalid")
        local, domain = normalized.split("@")
        if (
            not local
            or not domain
            or local.startswith(".")
            or local.endswith(".")
            or ".." in local
            or domain.startswith(".")
            or domain.endswith(".")
            or "." not in domain
            or ".." in domain
            or any(character.isspace() or not character.isprintable() for character in normalized)
        ):
            raise ValueError("email destination is invalid")
        return normalized
    normalized = re.sub(r"[\s()-]", "", normalized)
    if PHONE_PATTERN.fullmatch(normalized) is None:
        raise ValueError("phone destination must be E.164")
    if normalized.startswith("+82") is False:
        raise ValueError("sms and kakao destinations must use Korean E.164 (+82)")
    return normalized


def _masked_destination(channel: NotificationContactChannel, destination: str) -> str:
    if channel is NotificationContactChannel.EMAIL:
        local, domain = destination.split("@", 1)
        visible = local[:2]
        return f"{visible}{'*' * max(2, len(local) - len(visible))}@{domain}"
    return f"{destination[:3]}{'*' * max(4, len(destination) - 7)}{destination[-4:]}"


def _contact_response(contact: WorkspaceContactPoint) -> WorkspaceContactPointResponse:
    return WorkspaceContactPointResponse(
        channel=contact.channel,
        masked_destination=_masked_destination(contact.channel, contact.destination),
        enabled=contact.enabled,
        consented_at=_as_utc(contact.consented_at),
        updated_at=_as_utc(contact.updated_at),
    )


async def _cancel_pending_contact_deliveries(
    session: AsyncSession,
    account_id: UUID,
    channel: NotificationContactChannel,
) -> None:
    participant_ids = select(WorkspaceMembership.participant_id).where(
        WorkspaceMembership.account_id == account_id,
        WorkspaceMembership.revoked_at.is_(None),
    )
    delivery_channel = {
        NotificationContactChannel.EMAIL: NotificationChannel.EMAIL,
        NotificationContactChannel.SMS: NotificationChannel.SMS,
        NotificationContactChannel.KAKAO: NotificationChannel.KAKAO,
    }[channel]
    await session.execute(
        update(NotificationDelivery)
        .where(
            NotificationDelivery.recipient_participant_id.in_(participant_ids),
            NotificationDelivery.channel == delivery_channel,
            NotificationDelivery.status == NotificationStatus.PENDING,
        )
        .values(
            destination=f"{REVOKED_CONTACT_DESTINATION_PREFIX}{channel.value}",
            status=NotificationStatus.FAILED,
            last_error_code="consent_revoked",
            next_attempt_at=None,
            lock_token=None,
            locked_until=None,
        )
    )


async def upsert_contact_point(
    session: AsyncSession,
    account_id: UUID,
    channel: NotificationContactChannel,
    command: WorkspaceContactPointUpsert,
) -> WorkspaceContactPointResponse:
    destination = _normalized_destination(channel, command.destination)
    contact = await session.scalar(
        select(WorkspaceContactPoint)
        .where(
            WorkspaceContactPoint.account_id == account_id,
            WorkspaceContactPoint.channel == channel,
        )
        .with_for_update()
    )
    now = utc_now()
    if contact is None:
        contact = WorkspaceContactPoint(
            account_id=account_id,
            channel=channel,
            destination=destination,
            enabled=command.enabled,
            consented_at=now,
            updated_at=now,
        )
        session.add(contact)
    else:
        if contact.destination != destination or (contact.enabled and not command.enabled):
            await _cancel_pending_contact_deliveries(session, account_id, channel)
        contact.destination = destination
        contact.enabled = command.enabled
        contact.consented_at = now
        contact.updated_at = now
    await session.flush()
    return _contact_response(contact)


async def list_contact_points(
    session: AsyncSession,
    account_id: UUID,
) -> WorkspaceContactPointListResponse:
    contacts = (
        await session.scalars(
            select(WorkspaceContactPoint)
            .where(
                WorkspaceContactPoint.account_id == account_id,
                WorkspaceContactPoint.enabled.is_(True),
            )
            .order_by(WorkspaceContactPoint.channel, WorkspaceContactPoint.id)
        )
    ).all()
    return WorkspaceContactPointListResponse(
        contacts=tuple(_contact_response(contact) for contact in contacts)
    )


async def delete_contact_point(
    session: AsyncSession,
    account_id: UUID,
    channel: NotificationContactChannel,
) -> None:
    contact = await session.scalar(
        select(WorkspaceContactPoint)
        .where(
            WorkspaceContactPoint.account_id == account_id,
            WorkspaceContactPoint.channel == channel,
        )
        .with_for_update()
    )
    if contact is None or not contact.enabled:
        raise WorkspaceContactNotFoundError(channel)
    await _cancel_pending_contact_deliveries(session, account_id, channel)
    contact.destination = f"{REVOKED_CONTACT_DESTINATION_PREFIX}{channel.value}"
    contact.enabled = False
    contact.updated_at = utc_now()
    await session.flush()
