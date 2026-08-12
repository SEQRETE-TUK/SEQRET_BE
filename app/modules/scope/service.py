"""Application commands for immutable work-scope versions."""

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.actor import ParticipantRole
from app.contracts.primitives import utc_now
from app.modules.move_job.models import JobParticipant, Location, RoomZone
from app.modules.scope.models import ScopeApproval, ScopeVersion
from app.modules.scope.schemas import (
    ScopeApprovalResponse,
    ScopeApprovalResult,
    ScopeContent,
    ScopeVersionCreate,
    ScopeVersionResponse,
)


class ScopeResourceNotFoundError(LookupError):
    """Raised for a missing or cross-job scope resource."""


class ScopeVersionConflictError(ValueError):
    """Raised when the requested parent is no longer the current head."""


class ScopeApprovalConflictError(ValueError):
    """Raised for a duplicate, stale, or already locked confirmation."""


REQUIRED_APPROVAL_ROLES = (
    ParticipantRole.CUSTOMER,
    ParticipantRole.COMPANY_MANAGER,
)


async def _to_response(
    session: AsyncSession,
    version: ScopeVersion,
) -> ScopeVersionResponse:
    stored_roles = set(
        (
            await session.scalars(
                select(ScopeApproval.role).where(ScopeApproval.scope_version_id == version.id)
            )
        ).all()
    )
    return ScopeVersionResponse(
        id=version.id,
        job_id=version.job_id,
        parent_version_id=version.parent_version_id,
        sequence_number=version.sequence_number,
        content=ScopeContent.model_validate(version.content, strict=False),
        content_hash=version.content_hash,
        created_by_participant_id=version.created_by_participant_id,
        created_at=version.created_at,
        approval_roles=tuple(role for role in REQUIRED_APPROVAL_ROLES if role in stored_roles),
        locked_at=version.locked_at,
    )


async def create_scope_version(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
    command: ScopeVersionCreate,
) -> ScopeVersionResponse:
    participant = await session.scalar(
        select(JobParticipant.id).where(
            JobParticipant.id == participant_id,
            JobParticipant.job_id == job_id,
        )
    )
    if participant is None:
        raise ScopeResourceNotFoundError(job_id)

    requested_zone_ids = {item.room_zone_id for item in command.content.items}
    zone_statement = (
        select(RoomZone.id)
        .join(RoomZone.location)
        .where(
            RoomZone.id.in_(requested_zone_ids),
            Location.job_id == job_id,
        )
    )
    job_zone_ids = set((await session.scalars(zone_statement)).all())
    if job_zone_ids != requested_zone_ids:
        raise ScopeResourceNotFoundError(job_id)

    if command.parent_version_id is None:
        existing_root = await session.scalar(
            select(ScopeVersion.id).where(
                ScopeVersion.job_id == job_id,
                ScopeVersion.parent_version_id.is_(None),
            )
        )
        if existing_root is not None:
            raise ScopeVersionConflictError(job_id)
        sequence_number = 1
    else:
        parent = await session.scalar(
            select(ScopeVersion)
            .where(
                ScopeVersion.id == command.parent_version_id,
                ScopeVersion.job_id == job_id,
            )
            .with_for_update()
        )
        if parent is None:
            raise ScopeResourceNotFoundError(command.parent_version_id)
        if parent.locked_at is not None:
            raise ScopeVersionConflictError(parent.id)
        existing_child = await session.scalar(
            select(ScopeVersion.id).where(ScopeVersion.parent_version_id == parent.id)
        )
        if existing_child is not None:
            raise ScopeVersionConflictError(parent.id)
        sequence_number = parent.sequence_number + 1

    normalized_content = ScopeContent(
        schema_version=command.content.schema_version,
        items=tuple(
            sorted(
                command.content.items,
                key=lambda item: item.item_key,
            )
        ),
    )
    content_document: dict[str, Any] = normalized_content.model_dump(mode="json")
    canonical_json = json.dumps(
        content_document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    version = ScopeVersion(
        job_id=job_id,
        parent_version_id=command.parent_version_id,
        sequence_number=sequence_number,
        content=content_document,
        content_hash=hashlib.sha256(canonical_json.encode()).hexdigest(),
        created_by_participant_id=participant_id,
    )
    session.add(version)
    try:
        await session.flush()
    except IntegrityError as error:
        raise ScopeVersionConflictError(command.parent_version_id or job_id) from error
    return await _to_response(session, version)


async def list_scope_versions(
    session: AsyncSession,
    job_id: UUID,
) -> tuple[ScopeVersionResponse, ...]:
    statement = (
        select(ScopeVersion)
        .where(ScopeVersion.job_id == job_id)
        .order_by(ScopeVersion.sequence_number)
    )
    versions = (await session.scalars(statement)).all()
    return tuple([await _to_response(session, version) for version in versions])


async def approve_scope_version(
    session: AsyncSession,
    job_id: UUID,
    scope_version_id: UUID,
    participant_id: UUID,
    role: ParticipantRole,
) -> ScopeApprovalResult:
    participant = await session.scalar(
        select(JobParticipant.id).where(
            JobParticipant.id == participant_id,
            JobParticipant.job_id == job_id,
            JobParticipant.role == role,
        )
    )
    if participant is None:
        raise ScopeResourceNotFoundError(job_id)
    version = await session.scalar(
        select(ScopeVersion)
        .where(
            ScopeVersion.id == scope_version_id,
            ScopeVersion.job_id == job_id,
        )
        .with_for_update()
    )
    if version is None:
        raise ScopeResourceNotFoundError(scope_version_id)
    if version.locked_at is not None:
        raise ScopeApprovalConflictError(scope_version_id)
    if (
        await session.scalar(
            select(ScopeVersion.id).where(ScopeVersion.parent_version_id == version.id)
        )
        is not None
    ):
        raise ScopeApprovalConflictError(scope_version_id)

    existing_roles = set(
        (
            await session.scalars(
                select(ScopeApproval.role).where(ScopeApproval.scope_version_id == scope_version_id)
            )
        ).all()
    )
    if role in existing_roles:
        raise ScopeApprovalConflictError(scope_version_id)

    approval = ScopeApproval(
        scope_version_id=scope_version_id,
        participant_id=participant_id,
        role=role,
        approved_at=utc_now(),
    )
    session.add(approval)
    if existing_roles | {role} == set(REQUIRED_APPROVAL_ROLES):
        version.locked_at = approval.approved_at
    try:
        await session.flush()
    except IntegrityError as error:
        raise ScopeApprovalConflictError(scope_version_id) from error
    return ScopeApprovalResult(
        approval=ScopeApprovalResponse(
            id=approval.id,
            scope_version_id=approval.scope_version_id,
            participant_id=approval.participant_id,
            role=approval.role,
            approved_at=approval.approved_at,
        ),
        version=await _to_response(session, version),
    )
