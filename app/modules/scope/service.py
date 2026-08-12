"""Application commands for immutable work-scope versions."""

import hashlib
import json
from typing import Any
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.actor import ParticipantRole
from app.contracts.ai import AnalysisResult
from app.contracts.events import DomainEventType
from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.primitives import utc_now
from app.modules.capture.models import CaptureSession, MediaAsset
from app.modules.completion.models import AuditEventType, CompletionConfirmation
from app.modules.completion.service import add_audit_event
from app.modules.move_job.models import JobParticipant, Location, MoveJob, MoveJobStatus, RoomZone
from app.modules.scope.models import (
    ChangeRequest,
    ChangeRequestEvidence,
    ChangeRequestStatus,
    ScopeApproval,
    ScopeVersion,
)
from app.modules.scope.schemas import (
    ChangeDecisionCreate,
    ChangeRequestCreate,
    ChangeRequestResponse,
    ScopeApprovalResponse,
    ScopeApprovalResult,
    ScopeContent,
    ScopeItem,
    ScopeVersionCreate,
    ScopeVersionResponse,
)
from app.platform.event_bus import enqueue_domain_event


class ScopeResourceNotFoundError(LookupError):
    """Raised for a missing or cross-job scope resource."""


class ScopeVersionConflictError(ValueError):
    """Raised when the requested parent is no longer the current head."""


class ScopeApprovalConflictError(ValueError):
    """Raised for a duplicate, stale, or already locked confirmation."""


class AnalysisDraftInvalidError(ValueError):
    """Raised when an AI result cannot map safely to a work scope."""


class ChangeRequestConflictError(ValueError):
    """Raised when a change request cannot transition from its current state."""


class ChangeRequestInvalidError(ValueError):
    """Raised when proposed content and evidence do not describe the same change."""


REQUIRED_APPROVAL_ROLES = (
    ParticipantRole.CUSTOMER,
    ParticipantRole.COMPANY_MANAGER,
)


def _normalize_scope_content(content: ScopeContent) -> ScopeContent:
    return ScopeContent(
        schema_version=content.schema_version,
        items=tuple(sorted(content.items, key=lambda item: item.item_key)),
    )


async def _validate_scope_zones(
    session: AsyncSession,
    job_id: UUID,
    content: ScopeContent,
) -> None:
    requested_zone_ids = {item.room_zone_id for item in content.items}
    zone_statement = (
        select(RoomZone.id)
        .join(RoomZone.location)
        .where(
            RoomZone.id.in_(requested_zone_ids),
            Location.job_id == job_id,
        )
    )
    if set((await session.scalars(zone_statement)).all()) != requested_zone_ids:
        raise ScopeResourceNotFoundError(job_id)


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
        analysis_source=(
            AnalysisResult.model_validate(version.analysis_source, strict=False)
            if version.analysis_source is not None
            else None
        ),
    )


async def create_scope_version(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID | None,
    command: ScopeVersionCreate,
    *,
    analysis_source: AnalysisResult | None = None,
    from_locked_parent: bool = False,
) -> ScopeVersionResponse:
    is_analysis_import = analysis_source is not None
    if is_analysis_import == (participant_id is not None):
        raise AnalysisDraftInvalidError(job_id)
    if participant_id is not None:
        participant = await session.scalar(
            select(JobParticipant.id).where(
                JobParticipant.id == participant_id,
                JobParticipant.job_id == job_id,
            )
        )
        if participant is None:
            raise ScopeResourceNotFoundError(job_id)

    await _validate_scope_zones(session, job_id, command.content)

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
        if from_locked_parent != (parent.locked_at is not None):
            raise ScopeVersionConflictError(parent.id)
        if (
            not from_locked_parent
            and await session.scalar(
                select(ChangeRequest.id).where(ChangeRequest.result_scope_version_id == parent.id)
            )
            is not None
        ):
            raise ScopeVersionConflictError(parent.id)
        existing_child = await session.scalar(
            select(ScopeVersion.id).where(ScopeVersion.parent_version_id == parent.id)
        )
        if existing_child is not None:
            raise ScopeVersionConflictError(parent.id)
        sequence_number = parent.sequence_number + 1

    normalized_content = _normalize_scope_content(command.content)
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
        source_analysis_run_id=(analysis_source.analysis_run_id if analysis_source else None),
        source_capture_session_id=(analysis_source.capture_session_id if analysis_source else None),
        analysis_source=(analysis_source.model_dump(mode="json") if analysis_source else None),
        created_by_participant_id=participant_id,
    )
    session.add(version)
    try:
        await session.flush()
    except IntegrityError as error:
        raise ScopeVersionConflictError(command.parent_version_id or job_id) from error
    add_audit_event(
        session,
        job_id,
        AuditEventType.SCOPE_VERSION_CREATED,
        actor_participant_id=participant_id,
        payload={
            "scope_version_id": str(version.id),
            "parent_version_id": (
                str(version.parent_version_id) if version.parent_version_id else None
            ),
            "sequence_number": version.sequence_number,
            "content_hash": version.content_hash,
            "origin": "analysis" if analysis_source else "participant",
        },
    )
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
    *,
    trace_id: str | None = None,
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
    if version.locked_at is not None or version.analysis_source is not None:
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
    locked_now = existing_roles | {role} == set(REQUIRED_APPROVAL_ROLES)
    if locked_now:
        version.locked_at = approval.approved_at
    add_audit_event(
        session,
        job_id,
        AuditEventType.SCOPE_VERSION_APPROVED,
        actor_participant_id=participant_id,
        payload={"scope_version_id": str(scope_version_id), "role": role.value},
    )
    if locked_now:
        add_audit_event(
            session,
            job_id,
            AuditEventType.SCOPE_VERSION_LOCKED,
            actor_participant_id=participant_id,
            payload={
                "scope_version_id": str(scope_version_id),
                "content_hash": version.content_hash,
            },
        )
        enqueue_domain_event(
            session,
            DomainEventType.SCOPE_LOCKED_V1,
            job_id,
            actor_id=participant_id,
            trace_id=trace_id,
            payload={
                "scope_version_id": str(scope_version_id),
                "content_hash": version.content_hash,
            },
        )
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


async def import_analysis_draft(
    session: AsyncSession,
    job_id: UUID,
    result: AnalysisResult,
    parent_version_id: UUID | None = None,
) -> ScopeVersionResponse:
    if (
        await session.scalar(
            select(ScopeVersion.id).where(
                ScopeVersion.source_analysis_run_id == result.analysis_run_id
            )
        )
        is not None
    ):
        raise ScopeVersionConflictError(result.analysis_run_id)

    capture_session_id = await session.scalar(
        select(CaptureSession.id).where(
            CaptureSession.id == result.capture_session_id,
            CaptureSession.job_id == job_id,
        )
    )
    if capture_session_id is None:
        raise ScopeResourceNotFoundError(result.capture_session_id)

    result_items = result.draft_items + result.review_required_items
    item_keys = [item.item_key for item in result_items]
    if not result_items or len(item_keys) != len(set(item_keys)):
        raise AnalysisDraftInvalidError(result.analysis_run_id)
    if any(
        not item.source_media_asset_ids
        or len(item.source_media_asset_ids) != len(set(item.source_media_asset_ids))
        for item in result_items
    ):
        raise AnalysisDraftInvalidError(result.analysis_run_id)

    source_media_ids = {
        media_asset_id for item in result_items for media_asset_id in item.source_media_asset_ids
    }
    asset_statement = select(MediaAsset).where(
        MediaAsset.id.in_(source_media_ids),
        MediaAsset.capture_session_id == result.capture_session_id,
        MediaAsset.status.in_({MediaAssetStatus.UPLOADED, MediaAssetStatus.READY}),
    )
    assets = (await session.scalars(asset_statement)).all()
    assets_by_id = {asset.id: asset for asset in assets}
    if set(assets_by_id) != source_media_ids:
        raise AnalysisDraftInvalidError(result.analysis_run_id)

    scope_items = []
    for item in result_items:
        room_zone_ids = {
            assets_by_id[media_asset_id].room_zone_id
            for media_asset_id in item.source_media_asset_ids
        }
        if len(room_zone_ids) != 1:
            raise AnalysisDraftInvalidError(item.item_key)
        scope_items.append(
            ScopeItem(
                item_key=item.item_key,
                room_zone_id=room_zone_ids.pop(),
                description=item.description,
            )
        )

    return await create_scope_version(
        session,
        job_id,
        None,
        ScopeVersionCreate(
            parent_version_id=parent_version_id,
            content=ScopeContent(items=tuple(scope_items)),
        ),
        analysis_source=result,
    )


async def _change_request_response(
    session: AsyncSession,
    request: ChangeRequest,
) -> ChangeRequestResponse:
    evidence_ids = (
        await session.scalars(
            select(ChangeRequestEvidence.media_asset_id)
            .where(ChangeRequestEvidence.change_request_id == request.id)
            .order_by(ChangeRequestEvidence.media_asset_id)
        )
    ).all()
    return ChangeRequestResponse(
        id=request.id,
        job_id=request.job_id,
        base_scope_version_id=request.base_scope_version_id,
        requested_by_participant_id=request.requested_by_participant_id,
        description=request.description,
        proposed_content=ScopeContent.model_validate(request.proposed_content, strict=False),
        evidence_media_asset_ids=tuple(evidence_ids),
        status=request.status,
        clarification_requested_by_participant_id=(
            request.clarification_requested_by_participant_id
        ),
        clarification_request=request.clarification_request,
        clarification_requested_at=request.clarification_requested_at,
        explanation=request.explanation,
        explained_at=request.explained_at,
        decided_by_participant_id=request.decided_by_participant_id,
        decision_note=request.decision_note,
        decided_at=request.decided_at,
        result_scope_version_id=request.result_scope_version_id,
        created_at=request.created_at,
    )


async def create_change_request(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
    command: ChangeRequestCreate,
    *,
    trace_id: str | None = None,
) -> ChangeRequestResponse:
    job = await session.scalar(select(MoveJob).where(MoveJob.id == job_id).with_for_update())
    if job is None:
        raise ScopeResourceNotFoundError(job_id)
    if job.status in {MoveJobStatus.COMPLETED, MoveJobStatus.CANCELED}:
        raise ChangeRequestConflictError(job_id)
    participant = await session.scalar(
        select(JobParticipant.id).where(
            JobParticipant.id == participant_id,
            JobParticipant.job_id == job_id,
            JobParticipant.role == ParticipantRole.FIELD_WORKER,
        )
    )
    if participant is None:
        raise ScopeResourceNotFoundError(job_id)
    if (
        await session.scalar(
            select(CompletionConfirmation.id).where(CompletionConfirmation.job_id == job_id)
        )
        is not None
    ):
        raise ChangeRequestConflictError(job_id)
    base_version = await session.scalar(
        select(ScopeVersion)
        .where(
            ScopeVersion.id == command.base_scope_version_id,
            ScopeVersion.job_id == job_id,
        )
        .with_for_update()
    )
    if base_version is None:
        raise ScopeResourceNotFoundError(command.base_scope_version_id)
    if base_version.locked_at is None:
        raise ChangeRequestConflictError(command.base_scope_version_id)
    if (
        await session.scalar(
            select(ScopeVersion.id).where(ScopeVersion.parent_version_id == base_version.id)
        )
        is not None
    ):
        raise ChangeRequestConflictError(command.base_scope_version_id)

    await _validate_scope_zones(session, job_id, command.proposed_content)
    normalized_content = _normalize_scope_content(command.proposed_content)
    if normalized_content.model_dump(mode="json") == base_version.content:
        raise ChangeRequestInvalidError(job_id)
    evidence_statement = (
        select(MediaAsset)
        .join(CaptureSession, CaptureSession.id == MediaAsset.capture_session_id)
        .where(
            MediaAsset.id.in_(command.evidence_media_asset_ids),
            CaptureSession.job_id == job_id,
            CaptureSession.created_by_participant_id == participant_id,
            MediaAsset.media_purpose == MediaPurpose.CHANGE_EVIDENCE,
            MediaAsset.status.in_({MediaAssetStatus.UPLOADED, MediaAssetStatus.READY}),
        )
    )
    evidence = (await session.scalars(evidence_statement)).all()
    if {asset.id for asset in evidence} != set(command.evidence_media_asset_ids):
        raise ChangeRequestInvalidError(job_id)

    request = ChangeRequest(
        job_id=job_id,
        base_scope_version_id=command.base_scope_version_id,
        requested_by_participant_id=participant_id,
        description=command.description,
        proposed_content=normalized_content.model_dump(mode="json"),
    )
    session.add(request)
    await session.flush()
    session.add_all(
        ChangeRequestEvidence(change_request_id=request.id, media_asset_id=media_asset_id)
        for media_asset_id in command.evidence_media_asset_ids
    )
    add_audit_event(
        session,
        job_id,
        AuditEventType.CHANGE_REQUESTED,
        actor_participant_id=participant_id,
        payload={
            "change_request_id": str(request.id),
            "base_scope_version_id": str(request.base_scope_version_id),
            "evidence_media_asset_ids": sorted(
                str(media_asset_id) for media_asset_id in command.evidence_media_asset_ids
            ),
        },
    )
    evidence_ids = sorted(
        str(media_asset_id) for media_asset_id in command.evidence_media_asset_ids
    )
    event_evidence_ids: list[JsonValue] = [media_asset_id for media_asset_id in evidence_ids]
    enqueue_domain_event(
        session,
        DomainEventType.CHANGE_REQUESTED_V1,
        job_id,
        actor_id=participant_id,
        trace_id=trace_id,
        payload={
            "change_request_id": str(request.id),
            "base_scope_version_id": str(request.base_scope_version_id),
            "evidence_media_asset_ids": event_evidence_ids,
        },
    )
    await session.flush()
    return await _change_request_response(session, request)


async def list_change_requests(
    session: AsyncSession,
    job_id: UUID,
) -> tuple[ChangeRequestResponse, ...]:
    requests = (
        await session.scalars(
            select(ChangeRequest)
            .where(ChangeRequest.job_id == job_id)
            .order_by(ChangeRequest.created_at, ChangeRequest.id)
        )
    ).all()
    return tuple([await _change_request_response(session, request) for request in requests])


async def request_change_clarification(
    session: AsyncSession,
    job_id: UUID,
    change_request_id: UUID,
    participant_id: UUID,
    message: str,
) -> ChangeRequestResponse:
    await _require_job_participant(
        session,
        job_id,
        participant_id,
        {ParticipantRole.CUSTOMER, ParticipantRole.COMPANY_MANAGER},
    )
    request = await _load_change_request_for_update(session, job_id, change_request_id)
    if request.status is not ChangeRequestStatus.PENDING or request.clarification_request:
        raise ChangeRequestConflictError(change_request_id)
    request.status = ChangeRequestStatus.CLARIFICATION_REQUESTED
    request.clarification_requested_by_participant_id = participant_id
    request.clarification_request = message
    request.clarification_requested_at = utc_now()
    add_audit_event(
        session,
        job_id,
        AuditEventType.CHANGE_CLARIFICATION_REQUESTED,
        actor_participant_id=participant_id,
        payload={"change_request_id": str(change_request_id)},
    )
    await session.flush()
    return await _change_request_response(session, request)


async def explain_change_request(
    session: AsyncSession,
    job_id: UUID,
    change_request_id: UUID,
    participant_id: UUID,
    explanation: str,
) -> ChangeRequestResponse:
    await _require_job_participant(
        session,
        job_id,
        participant_id,
        {ParticipantRole.FIELD_WORKER},
    )
    request = await _load_change_request_for_update(session, job_id, change_request_id)
    if (
        request.status is not ChangeRequestStatus.CLARIFICATION_REQUESTED
        or request.requested_by_participant_id != participant_id
    ):
        raise ChangeRequestConflictError(change_request_id)
    request.status = ChangeRequestStatus.PENDING
    request.explanation = explanation
    request.explained_at = utc_now()
    add_audit_event(
        session,
        job_id,
        AuditEventType.CHANGE_EXPLAINED,
        actor_participant_id=participant_id,
        payload={"change_request_id": str(change_request_id)},
    )
    await session.flush()
    return await _change_request_response(session, request)


async def decide_change_request(
    session: AsyncSession,
    job_id: UUID,
    change_request_id: UUID,
    participant_id: UUID,
    command: ChangeDecisionCreate,
) -> ChangeRequestResponse:
    await _require_job_participant(
        session,
        job_id,
        participant_id,
        {ParticipantRole.CUSTOMER, ParticipantRole.COMPANY_MANAGER},
    )
    request = await _load_change_request_for_update(session, job_id, change_request_id)
    if request.status is not ChangeRequestStatus.PENDING:
        raise ChangeRequestConflictError(change_request_id)
    now = utc_now()
    if command.decision == "reject":
        request.status = ChangeRequestStatus.REJECTED
    else:
        try:
            result_version = await create_scope_version(
                session,
                job_id,
                participant_id,
                ScopeVersionCreate(
                    parent_version_id=request.base_scope_version_id,
                    content=ScopeContent.model_validate(request.proposed_content, strict=False),
                ),
                from_locked_parent=True,
            )
        except ScopeVersionConflictError as error:
            raise ChangeRequestConflictError(change_request_id) from error
        request.status = ChangeRequestStatus.APPROVED
        request.result_scope_version_id = result_version.id
    request.decided_by_participant_id = participant_id
    request.decision_note = command.note
    request.decided_at = now
    add_audit_event(
        session,
        job_id,
        (
            AuditEventType.CHANGE_APPROVED
            if request.status is ChangeRequestStatus.APPROVED
            else AuditEventType.CHANGE_REJECTED
        ),
        actor_participant_id=participant_id,
        payload={
            "change_request_id": str(change_request_id),
            "result_scope_version_id": (
                str(request.result_scope_version_id) if request.result_scope_version_id else None
            ),
        },
    )
    await session.flush()
    return await _change_request_response(session, request)


async def _load_change_request_for_update(
    session: AsyncSession,
    job_id: UUID,
    change_request_id: UUID,
) -> ChangeRequest:
    request = await session.scalar(
        select(ChangeRequest)
        .where(ChangeRequest.id == change_request_id, ChangeRequest.job_id == job_id)
        .with_for_update()
    )
    if request is None:
        raise ScopeResourceNotFoundError(change_request_id)
    return request


async def _require_job_participant(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
    roles: set[ParticipantRole],
) -> None:
    if (
        await session.scalar(
            select(JobParticipant.id).where(
                JobParticipant.id == participant_id,
                JobParticipant.job_id == job_id,
                JobParticipant.role.in_(roles),
            )
        )
        is None
    ):
        raise ScopeResourceNotFoundError(job_id)
