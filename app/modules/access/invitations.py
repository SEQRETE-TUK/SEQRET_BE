"""Role invitation application commands and views."""

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.contracts.actor import ActorContext, ParticipantRole
from app.contracts.primitives import utc_now
from app.modules.access.models import (
    InvitationStatus,
    ParticipantAccessToken,
    ParticipantInvitation,
)
from app.modules.access.schemas import (
    ActorSelfResponse,
    InvitationCreate,
    InvitationIssuedResponse,
    InvitationListResponse,
    InvitationResponse,
)
from app.modules.access.service import (
    InvalidAccessTokenError,
    access_link_matches_secret,
    issue_access_link,
    revoke_access_link,
)
from app.modules.completion.models import AuditEventType
from app.modules.completion.service import add_audit_event
from app.modules.move_job.models import JobParticipant, MoveJob


class InvitationNotFoundError(LookupError):
    """Raised when an invitation is outside the actor's visible job scope."""


class InvitationConflictError(RuntimeError):
    """Raised when an invitation command conflicts with its current lifecycle."""


class InvitationRoleError(PermissionError):
    """Raised when an actor tries to invite a role outside the onboarding chain."""


class ActorAccessLinkNotFoundError(LookupError):
    """Raised if an authenticated actor's active link disappears concurrently."""


_INVITABLE_ROLE = {
    ParticipantRole.CUSTOMER: ParticipantRole.COMPANY_MANAGER,
    ParticipantRole.COMPANY_MANAGER: ParticipantRole.FIELD_WORKER,
}

_ROLE_PERMISSIONS = {
    ParticipantRole.CUSTOMER: (
        "job:read",
        "capture:write",
        "scope:review",
        "invitation:company_manager:manage",
        "completion:confirm",
    ),
    ParticipantRole.COMPANY_MANAGER: (
        "job:read",
        "scope:propose",
        "invitation:field_worker:manage",
        "dispatch:manage",
        "completion:request",
    ),
    ParticipantRole.FIELD_WORKER: (
        "job:read",
        "field:check_in",
        "field:issue",
        "completion:submit",
    ),
}

_PENDING_INVITATION_PERMISSIONS = (
    "invitation:read",
    "invitation:accept",
    "invitation:decline",
)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def _materialize_expiry(
    invitation: ParticipantInvitation,
    *,
    now: datetime,
) -> None:
    if invitation.status is InvitationStatus.PENDING and _as_utc(invitation.expires_at) <= now:
        invitation.status = InvitationStatus.EXPIRED


def _to_response(invitation: ParticipantInvitation) -> InvitationResponse:
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


async def _load_active_access_link(
    session: AsyncSession,
    participant_id: UUID,
    *,
    now: datetime,
) -> ParticipantAccessToken | None:
    statement = (
        select(ParticipantAccessToken)
        .where(
            ParticipantAccessToken.participant_id == participant_id,
            ParticipantAccessToken.revoked_at.is_(None),
            ParticipantAccessToken.expires_at > now + timedelta(seconds=1),
        )
        .options(joinedload(ParticipantAccessToken.participant))
    )
    return (await session.scalars(statement)).one_or_none()


async def create_invitation(
    session: AsyncSession,
    job_id: UUID,
    actor: ActorContext,
    command: InvitationCreate,
) -> InvitationIssuedResponse:
    """Create the next role and issue its first pending capability atomically."""

    expected_role = _INVITABLE_ROLE.get(cast(ParticipantRole, actor.participant_role))
    if expected_role is None or command.role is not expected_role:
        raise InvitationRoleError
    issuer_id = cast(UUID, actor.participant_id)
    locked_job_id = await session.scalar(
        select(MoveJob.id).where(MoveJob.id == job_id).with_for_update()
    )
    if locked_job_id is None:
        raise InvitationNotFoundError(job_id)
    issuer_access_link = await _load_active_access_link(
        session,
        issuer_id,
        now=utc_now(),
    )
    if issuer_access_link is None:
        raise InvitationConflictError("issuer access link is unavailable")
    existing_invitation = await session.scalar(
        select(ParticipantInvitation.id).where(
            ParticipantInvitation.job_id == job_id,
            ParticipantInvitation.role == command.role,
        )
    )
    existing_participant = await session.scalar(
        select(JobParticipant.id).where(
            JobParticipant.job_id == job_id,
            JobParticipant.role == command.role,
        )
    )
    if existing_invitation is not None or existing_participant is not None:
        raise InvitationConflictError("role already provisioned")

    display_name = (
        command.display_name
        or {
            ParticipantRole.COMPANY_MANAGER: "이사업체 담당자",
            ParticipantRole.FIELD_WORKER: "현장기사",
        }[command.role]
    )
    invitee = JobParticipant(
        job_id=job_id,
        role=command.role,
        display_name=display_name,
    )
    session.add(invitee)
    await session.flush()
    invitation_id = uuid4()
    access_link = await issue_access_link(
        session,
        invitee,
        actor_participant_id=issuer_id,
        operation="invitation_issued",
        invitation_id=invitation_id,
        expires_at_limit=issuer_access_link.expires_at,
    )
    issued_at = utc_now()
    invitation = ParticipantInvitation(
        id=invitation_id,
        job_id=job_id,
        issuer_participant_id=issuer_id,
        invitee_participant_id=invitee.id,
        access_link_id=access_link.id,
        role=command.role,
        status=InvitationStatus.PENDING,
        issued_at=issued_at,
        expires_at=access_link.expires_at,
    )
    invitation.invitee = invitee
    stored_access_link = await session.get(ParticipantAccessToken, access_link.id)
    assert stored_access_link is not None
    invitation.access_link = stored_access_link
    session.add(invitation)
    await session.flush()
    return InvitationIssuedResponse(
        invitation=_to_response(invitation),
        access_link=access_link,
    )


async def list_invitations(
    session: AsyncSession,
    job_id: UUID,
    actor: ActorContext,
) -> InvitationListResponse:
    """List invitations issued by or addressed to the current participant."""

    participant_id = cast(UUID, actor.participant_id)
    statement = (
        select(ParticipantInvitation)
        .where(
            ParticipantInvitation.job_id == job_id,
            or_(
                ParticipantInvitation.issuer_participant_id == participant_id,
                ParticipantInvitation.invitee_participant_id == participant_id,
            ),
        )
        .options(joinedload(ParticipantInvitation.invitee))
        .order_by(ParticipantInvitation.issued_at, ParticipantInvitation.id)
    )
    invitations = tuple((await session.scalars(statement)).all())
    now = utc_now()
    for invitation in invitations:
        _materialize_expiry(invitation, now=now)
    return InvitationListResponse(
        invitations=tuple(_to_response(invitation) for invitation in invitations)
    )


async def _load_invitation_for_invitee(
    session: AsyncSession,
    job_id: UUID,
    invitation_id: UUID,
    actor: ActorContext,
) -> ParticipantInvitation:
    statement = (
        select(ParticipantInvitation)
        .where(
            ParticipantInvitation.id == invitation_id,
            ParticipantInvitation.job_id == job_id,
            ParticipantInvitation.invitee_participant_id == cast(UUID, actor.participant_id),
        )
        .options(
            selectinload(ParticipantInvitation.invitee),
            selectinload(ParticipantInvitation.access_link).selectinload(
                ParticipantAccessToken.participant
            ),
        )
        .with_for_update()
    )
    invitation = await session.scalar(statement)
    if invitation is None:
        raise InvitationNotFoundError(invitation_id)
    _materialize_expiry(invitation, now=utc_now())
    return invitation


async def _load_invitation_for_issuer(
    session: AsyncSession,
    job_id: UUID,
    invitation_id: UUID,
    actor: ActorContext,
) -> ParticipantInvitation:
    statement = (
        select(ParticipantInvitation)
        .where(
            ParticipantInvitation.id == invitation_id,
            ParticipantInvitation.job_id == job_id,
            ParticipantInvitation.issuer_participant_id == cast(UUID, actor.participant_id),
        )
        .options(
            selectinload(ParticipantInvitation.invitee),
            selectinload(ParticipantInvitation.access_link).selectinload(
                ParticipantAccessToken.participant
            ),
        )
        .with_for_update()
    )
    invitation = await session.scalar(statement)
    if invitation is None:
        raise InvitationNotFoundError(invitation_id)
    _materialize_expiry(invitation, now=utc_now())
    return invitation


async def _revoke_invitations_issued_by(
    session: AsyncSession,
    *,
    job_id: UUID,
    issuer_participant_id: UUID,
    actor_participant_id: UUID,
    now: datetime,
) -> None:
    descendants = tuple(
        (
            await session.scalars(
                select(ParticipantInvitation)
                .where(
                    ParticipantInvitation.job_id == job_id,
                    ParticipantInvitation.issuer_participant_id == issuer_participant_id,
                )
                .options(
                    selectinload(ParticipantInvitation.invitee),
                    selectinload(ParticipantInvitation.access_link).selectinload(
                        ParticipantAccessToken.participant
                    ),
                )
                .with_for_update()
            )
        ).all()
    )
    for descendant in descendants:
        await _revoke_invitation_tree(
            session,
            descendant,
            actor_participant_id=actor_participant_id,
            now=now,
        )


async def _revoke_invitation_tree(
    session: AsyncSession,
    invitation: ParticipantInvitation,
    *,
    actor_participant_id: UUID,
    now: datetime,
) -> None:
    if invitation.role is ParticipantRole.COMPANY_MANAGER:
        await _revoke_invitations_issued_by(
            session,
            job_id=invitation.job_id,
            issuer_participant_id=invitation.invitee_participant_id,
            actor_participant_id=actor_participant_id,
            now=now,
        )
    invitation.status = InvitationStatus.REVOKED
    invitation.resolved_at = now
    await revoke_access_link(
        session,
        invitation.access_link,
        actor_participant_id,
    )


async def revoke_access_link_tree(
    session: AsyncSession,
    access_link: ParticipantAccessToken,
    actor_participant_id: UUID,
) -> None:
    """Revoke one capability and every invitation delegated from its participant."""

    now = utc_now()
    invitation = await session.scalar(
        select(ParticipantInvitation)
        .where(ParticipantInvitation.access_link_id == access_link.id)
        .options(
            selectinload(ParticipantInvitation.invitee),
            selectinload(ParticipantInvitation.access_link).selectinload(
                ParticipantAccessToken.participant
            ),
        )
        .with_for_update()
    )
    if invitation is not None:
        await _revoke_invitation_tree(
            session,
            invitation,
            actor_participant_id=actor_participant_id,
            now=now,
        )
    else:
        await _revoke_invitations_issued_by(
            session,
            job_id=access_link.participant.job_id,
            issuer_participant_id=access_link.participant_id,
            actor_participant_id=actor_participant_id,
            now=now,
        )
        await revoke_access_link(session, access_link, actor_participant_id)
    await session.flush()


async def accept_invitation(
    session: AsyncSession,
    job_id: UUID,
    invitation_id: UUID,
    actor: ActorContext,
    *,
    secret: str,
) -> InvitationResponse:
    invitation = await _load_invitation_for_invitee(session, job_id, invitation_id, actor)
    if not access_link_matches_secret(invitation.access_link, secret):
        raise InvalidAccessTokenError
    if invitation.status is InvitationStatus.ACCEPTED:
        return _to_response(invitation)
    if invitation.status is not InvitationStatus.PENDING:
        raise InvitationConflictError("invitation is not pending")
    now = utc_now()
    invitation.status = InvitationStatus.ACCEPTED
    invitation.resolved_at = now
    add_audit_event(
        session,
        invitation.job_id,
        AuditEventType.PARTICIPANT_CONNECTED,
        actor_participant_id=invitation.invitee_participant_id,
        payload={
            "access_link_id": str(invitation.access_link_id),
            "participant_id": str(invitation.invitee_participant_id),
            "role": invitation.role.value,
            "operation": "invitation_accepted",
            "invitation_id": str(invitation.id),
        },
    )
    await session.flush()
    return _to_response(invitation)


async def decline_invitation(
    session: AsyncSession,
    job_id: UUID,
    invitation_id: UUID,
    actor: ActorContext,
    *,
    secret: str,
) -> InvitationResponse:
    invitation = await _load_invitation_for_invitee(session, job_id, invitation_id, actor)
    if not access_link_matches_secret(invitation.access_link, secret):
        raise InvalidAccessTokenError
    if invitation.status is not InvitationStatus.PENDING:
        raise InvitationConflictError("invitation is not pending")
    invitation.status = InvitationStatus.DECLINED
    invitation.resolved_at = utc_now()
    await revoke_access_link(
        session,
        invitation.access_link,
        invitation.invitee_participant_id,
    )
    await session.flush()
    return _to_response(invitation)


async def revoke_invitation(
    session: AsyncSession,
    job_id: UUID,
    invitation_id: UUID,
    actor: ActorContext,
) -> InvitationResponse:
    invitation = await _load_invitation_for_issuer(session, job_id, invitation_id, actor)
    if invitation.status is InvitationStatus.REVOKED:
        return _to_response(invitation)
    actor_participant_id = cast(UUID, actor.participant_id)
    now = utc_now()
    await _revoke_invitation_tree(
        session,
        invitation,
        actor_participant_id=actor_participant_id,
        now=now,
    )
    await session.flush()
    return _to_response(invitation)


async def reissue_invitation(
    session: AsyncSession,
    job_id: UUID,
    invitation_id: UUID,
    actor: ActorContext,
) -> InvitationIssuedResponse:
    invitation = await _load_invitation_for_issuer(session, job_id, invitation_id, actor)
    issuer_id = cast(UUID, actor.participant_id)
    now = utc_now()
    issuer_access_link = await _load_active_access_link(
        session,
        issuer_id,
        now=now,
    )
    if issuer_access_link is None:
        raise InvitationConflictError("issuer access link is unavailable")
    await _revoke_invitations_issued_by(
        session,
        job_id=invitation.job_id,
        issuer_participant_id=invitation.invitee_participant_id,
        actor_participant_id=issuer_id,
        now=now,
    )
    await revoke_access_link(
        session,
        invitation.access_link,
        issuer_id,
    )
    access_link = await issue_access_link(
        session,
        invitation.invitee,
        actor_participant_id=issuer_id,
        operation="invitation_reissued",
        invitation_id=invitation.id,
        expires_at_limit=issuer_access_link.expires_at,
    )
    invitation.access_link_id = access_link.id
    stored_access_link = await session.get(ParticipantAccessToken, access_link.id)
    assert stored_access_link is not None
    invitation.access_link = stored_access_link
    invitation.status = InvitationStatus.PENDING
    invitation.issued_at = now
    invitation.expires_at = access_link.expires_at
    invitation.resolved_at = None
    await session.flush()
    return InvitationIssuedResponse(
        invitation=_to_response(invitation),
        access_link=access_link,
    )


async def get_actor_self(
    session: AsyncSession,
    actor: ActorContext,
) -> ActorSelfResponse:
    participant_id = cast(UUID, actor.participant_id)
    now = utc_now()
    access_link = await _load_active_access_link(
        session,
        participant_id,
        now=now,
    )
    if access_link is None:
        raise ActorAccessLinkNotFoundError(participant_id)
    invitation = await session.scalar(
        select(ParticipantInvitation)
        .where(ParticipantInvitation.invitee_participant_id == participant_id)
        .options(joinedload(ParticipantInvitation.invitee))
    )
    if invitation is not None:
        _materialize_expiry(invitation, now=now)
    permissions = (
        _PENDING_INVITATION_PERMISSIONS
        if invitation is not None and invitation.status is not InvitationStatus.ACCEPTED
        else _ROLE_PERMISSIONS[cast(ParticipantRole, actor.participant_role)]
    )
    return ActorSelfResponse(
        job_id=access_link.participant.job_id,
        participant_id=participant_id,
        role=access_link.participant.role,
        display_name=access_link.participant.display_name,
        permissions=permissions,
        expires_at=_as_utc(access_link.expires_at),
        invitation=_to_response(invitation) if invitation is not None else None,
    )
