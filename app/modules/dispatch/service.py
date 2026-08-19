"""Dispatch setup, atomic assignment, field brief, and replay-safe check-in."""

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.contracts.actor import ParticipantRole
from app.contracts.events import DomainEventType
from app.contracts.model import ContractModel
from app.contracts.primitives import utc_now
from app.modules.completion.models import CompletionSubmission
from app.modules.dispatch.models import DispatchPlan, DispatchSetup, FieldCheckIn
from app.modules.dispatch.schemas import (
    DispatchCheck,
    DispatchCheckStatus,
    DispatchConfirmCreate,
    DispatchRequirements,
    DispatchSetupCreate,
    DispatchStatus,
    DispatchVehicleOption,
    DispatchView,
    DispatchWorkerOption,
    FieldBriefCheckItem,
    FieldBriefView,
    FieldBriefWorker,
    FieldCheckInCreate,
    FieldCheckInResponse,
)
from app.modules.field_change.models import ChangeProposalDetail
from app.modules.move_job.models import (
    JobParticipant,
    LocationKind,
    MoveJob,
    MoveJobStatus,
)
from app.modules.scope.models import ChangeRequest, ChangeRequestStatus, ScopeVersion
from app.modules.scope.schemas import ScopeContent
from app.modules.scope_review.models import ScopeProposal, ScopeProposalStatus
from app.modules.scope_review.schemas import QuoteAdjustment, QuoteSnapshot, ScopeReviewJobHeader
from app.platform.event_bus import enqueue_domain_event


class DispatchNotFoundError(LookupError):
    """Raised when a job-scoped dispatch resource does not exist."""


class DispatchConflictError(ValueError):
    """Raised for stale snapshots, invalid selections, and lifecycle conflicts."""


class DispatchInvalidError(ValueError):
    """Raised when a candidate snapshot cannot satisfy its public contract."""


FIELD_SERVICE_TIMEZONE = timezone(timedelta(hours=9), name="Asia/Seoul")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _command_hash(value: ContractModel) -> str:
    encoded = json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _confirmed_quote(proposal: ScopeProposal | None) -> QuoteSnapshot | None:
    if proposal is None:
        return None
    if proposal.status is not ScopeProposalStatus.CONFIRMED:
        raise DispatchConflictError(proposal.id)
    return QuoteSnapshot(
        base_amount_krw=proposal.base_amount_krw,
        adjustments=tuple(
            QuoteAdjustment.model_validate(value, strict=False) for value in proposal.adjustments
        ),
        total_amount_krw=proposal.total_amount_krw,
    )


def _approved_change_quote(detail: ChangeProposalDetail) -> QuoteSnapshot:
    return QuoteSnapshot.model_validate(
        {
            "base_amount_krw": detail.base_amount_krw,
            "adjustments": detail.adjustments,
            "total_amount_krw": detail.total_amount_krw,
        },
        strict=False,
    )


async def _field_brief_quote_context(
    session: AsyncSession,
    job_id: UUID,
    scope_version_id: UUID,
) -> tuple[QuoteSnapshot | None, ScopeProposal | None]:
    """Resolve the effective quote and inherited classifications for a locked scope."""

    proposal = await session.scalar(
        select(ScopeProposal).where(
            ScopeProposal.job_id == job_id,
            ScopeProposal.result_scope_version_id == scope_version_id,
        )
    )
    change_row = (
        (
            await session.execute(
                select(ChangeProposalDetail, ChangeRequest)
                .join(
                    ChangeRequest,
                    ChangeRequest.id == ChangeProposalDetail.change_request_id,
                )
                .where(
                    ChangeRequest.job_id == job_id,
                    ChangeRequest.result_scope_version_id == scope_version_id,
                    ChangeRequest.status == ChangeRequestStatus.APPROVED,
                )
            )
        )
        .tuples()
        .one_or_none()
    )
    if proposal is not None and change_row is not None:
        raise DispatchConflictError(scope_version_id)
    if proposal is not None:
        return _confirmed_quote(proposal), proposal
    if change_row is None:
        return None, None

    detail, _ = change_row
    agreement_proposal = await session.scalar(
        select(ScopeProposal)
        .where(
            ScopeProposal.job_id == job_id,
            ScopeProposal.status == ScopeProposalStatus.CONFIRMED,
        )
        .order_by(ScopeProposal.confirmed_at.desc(), ScopeProposal.id.desc())
        .limit(1)
    )
    if agreement_proposal is None:
        raise DispatchConflictError(scope_version_id)
    return _approved_change_quote(detail), agreement_proposal


async def _require_participant(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
    role: ParticipantRole,
) -> JobParticipant:
    participant = await session.scalar(
        select(JobParticipant).where(
            JobParticipant.id == participant_id,
            JobParticipant.job_id == job_id,
            JobParticipant.role == role,
        )
    )
    if participant is None:
        raise DispatchNotFoundError(job_id)
    return participant


async def _load_job(
    session: AsyncSession,
    job_id: UUID,
    *,
    lock: bool = False,
) -> MoveJob:
    statement = (
        select(MoveJob)
        .where(MoveJob.id == job_id)
        .options(selectinload(MoveJob.participants), selectinload(MoveJob.locations))
    )
    if lock:
        statement = statement.with_for_update()
    job = await session.scalar(statement)
    if job is None:
        raise DispatchNotFoundError(job_id)
    return job


def _require_open_scheduled_job(job: MoveJob) -> datetime:
    if job.status in {MoveJobStatus.COMPLETED, MoveJobStatus.CANCELED}:
        raise DispatchConflictError(job.id)
    if job.scheduled_at is None:
        raise DispatchConflictError(job.id)
    return _aware(job.scheduled_at)


async def _current_locked_scope(
    session: AsyncSession,
    job_id: UUID,
    scope_version_id: UUID,
    *,
    lock: bool = False,
) -> ScopeVersion:
    statement = select(ScopeVersion).where(
        ScopeVersion.id == scope_version_id,
        ScopeVersion.job_id == job_id,
    )
    if lock:
        statement = statement.with_for_update()
    version = await session.scalar(statement)
    if version is None:
        raise DispatchNotFoundError(scope_version_id)
    if version.locked_at is None:
        raise DispatchConflictError(scope_version_id)
    if (
        await session.scalar(
            select(ScopeVersion.id).where(ScopeVersion.parent_version_id == version.id)
        )
        is not None
    ):
        raise DispatchConflictError(scope_version_id)
    return version


def _vehicle_options(setup: DispatchSetup) -> tuple[DispatchVehicleOption, ...]:
    return tuple(
        DispatchVehicleOption.model_validate(option, strict=False)
        for option in setup.vehicle_options
    )


def _worker_options(setup: DispatchSetup) -> tuple[DispatchWorkerOption, ...]:
    return tuple(
        DispatchWorkerOption.model_validate(option, strict=False) for option in setup.worker_options
    )


def _requirements(setup: DispatchSetup) -> DispatchRequirements:
    return DispatchRequirements(
        start_at=_aware(setup.start_at),
        expected_duration_minutes=setup.expected_duration_minutes,
        required_vehicle_capacity_m2=setup.required_vehicle_capacity_m2,
        required_worker_count=setup.required_worker_count,
        required_skills=tuple(setup.required_skills),
        required_certifications=tuple(setup.required_certifications),
    )


def _job_header(job: MoveJob, viewer: JobParticipant) -> ScopeReviewJobHeader:
    participant_names = {
        participant.role: participant.display_name for participant in job.participants
    }
    location_names = {location.kind: location.label for location in job.locations}
    return ScopeReviewJobHeader(
        job_id=job.id,
        job_code=f"MOVE-{job.id.hex[:8].upper()}",
        title=job.title,
        scheduled_at=job.scheduled_at,
        customer_display_name=participant_names.get(ParticipantRole.CUSTOMER),
        company_display_name=participant_names.get(ParticipantRole.COMPANY_MANAGER),
        viewer_display_name=viewer.display_name,
        viewer_role=viewer.role,
        origin_summary=location_names.get(LocationKind.ORIGIN),
        destination_summary=location_names.get(LocationKind.DESTINATION),
    )


def _snapshot_options(
    command: DispatchSetupCreate,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    vehicles: list[dict[str, object]] = []
    for option in command.vehicles:
        vehicles.append(
            {
                "id": str(uuid4()),
                **option.model_dump(mode="json"),
            }
        )
    workers: list[dict[str, object]] = []
    for worker_option in command.workers:
        workers.append(
            {
                "id": str(uuid4()),
                **worker_option.model_dump(mode="json"),
            }
        )
    return vehicles, workers


async def create_dispatch_setup(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
    command: DispatchSetupCreate,
) -> DispatchSetup:
    """Persist one immutable manager-supplied resource snapshot per job."""

    command_hash = _command_hash(command)
    job = await _load_job(session, job_id, lock=True)
    existing = await session.scalar(
        select(DispatchSetup).where(DispatchSetup.job_id == job_id).with_for_update()
    )
    if existing is not None:
        if (
            existing.client_reference == command.client_reference
            and existing.created_by_participant_id == participant_id
            and existing.command_hash == command_hash
        ):
            return existing
        raise DispatchConflictError(job_id)

    start_at = _require_open_scheduled_job(job)
    await _require_participant(
        session,
        job_id,
        participant_id,
        ParticipantRole.COMPANY_MANAGER,
    )
    await _current_locked_scope(
        session,
        job_id,
        command.source_scope_version_id,
        lock=True,
    )
    field_worker = next(
        (stored for stored in job.participants if stored.role is ParticipantRole.FIELD_WORKER),
        None,
    )
    if field_worker is None:
        raise DispatchNotFoundError(job_id)
    mapped_participant_ids = tuple(
        worker.participant_id for worker in command.workers if worker.participant_id is not None
    )
    if mapped_participant_ids != (field_worker.id,):
        raise DispatchInvalidError(field_worker.id)
    if command.required_worker_count > len(command.workers):
        raise DispatchInvalidError(job_id)

    vehicles, workers = _snapshot_options(command)
    setup = DispatchSetup(
        job_id=job_id,
        client_reference=command.client_reference,
        source_scope_version_id=command.source_scope_version_id,
        created_by_participant_id=participant_id,
        command_hash=command_hash,
        start_at=start_at,
        expected_duration_minutes=command.expected_duration_minutes,
        required_vehicle_capacity_m2=command.required_vehicle_capacity_m2,
        required_worker_count=command.required_worker_count,
        required_skills=list(command.required_skills),
        required_certifications=list(command.required_certifications),
        check_in_items=[item.model_dump(mode="json") for item in command.check_in_items],
        completion_check_items=[
            item.model_dump(mode="json") for item in command.completion_check_items
        ],
        origin_conditions=list(command.origin_conditions),
        safety_notice=command.safety_notice,
        vehicle_options=vehicles,
        worker_options=workers,
        created_at=utc_now(),
    )
    session.add(setup)
    try:
        await session.flush()
    except IntegrityError as error:
        raise DispatchConflictError(job_id) from error
    return setup


def _coverage_check(
    key: str,
    label: str,
    required: set[str],
    available: set[str],
) -> DispatchCheck:
    missing = sorted(required - available)
    return DispatchCheck(
        key=key,
        status=DispatchCheckStatus.FAIL if missing else DispatchCheckStatus.PASS,
        detail=f"{label}: {', '.join(missing)}" if missing else f"{label}: 충족",
    )


def _dispatch_checks(
    setup: DispatchSetup,
    vehicles: tuple[DispatchVehicleOption, ...],
    workers: tuple[DispatchWorkerOption, ...],
    plan: DispatchPlan | None,
) -> tuple[DispatchCheck, ...]:
    if plan is None:
        eligible_vehicles = tuple(
            vehicle
            for vehicle in vehicles
            if vehicle.available and vehicle.capacity_m2 >= setup.required_vehicle_capacity_m2
        )
        eligible_workers = tuple(worker for worker in workers if worker.available)
        skills = {skill for worker in eligible_workers for skill in worker.skills}
        certifications = {
            certification for worker in eligible_workers for certification in worker.certifications
        }
        return (
            DispatchCheck(
                key="vehicle_availability",
                status=(
                    DispatchCheckStatus.PASS if eligible_vehicles else DispatchCheckStatus.FAIL
                ),
                detail=(
                    "가용 용량 충족 차량 있음" if eligible_vehicles else "가용 용량 충족 차량 없음"
                ),
            ),
            DispatchCheck(
                key="worker_availability",
                status=(
                    DispatchCheckStatus.PASS
                    if len(eligible_workers) >= setup.required_worker_count
                    else DispatchCheckStatus.FAIL
                ),
                detail=f"가용 작업자 {len(eligible_workers)}/{setup.required_worker_count}",
            ),
            _coverage_check(
                "required_skills",
                "필수 기술",
                set(setup.required_skills),
                skills,
            ),
            _coverage_check(
                "required_certifications",
                "필수 자격",
                set(setup.required_certifications),
                certifications,
            ),
        )

    vehicle = next(
        (option for option in vehicles if option.id == plan.vehicle_option_id),
        None,
    )
    worker_map = {option.id: option for option in workers}
    selected = tuple(
        worker_map[UUID(value)]
        for value in plan.selected_worker_option_ids
        if UUID(value) in worker_map
    )
    skills = {skill for worker in selected for skill in worker.skills}
    certifications = {value for worker in selected for value in worker.certifications}
    return (
        DispatchCheck(
            key="vehicle_availability",
            status=(
                DispatchCheckStatus.PASS
                if vehicle is not None
                and vehicle.available
                and vehicle.capacity_m2 >= setup.required_vehicle_capacity_m2
                else DispatchCheckStatus.FAIL
            ),
            detail="선택 차량 일정·용량 충족",
        ),
        DispatchCheck(
            key="worker_availability",
            status=(
                DispatchCheckStatus.PASS
                if len(selected) == setup.required_worker_count
                and all(worker.available for worker in selected)
                else DispatchCheckStatus.FAIL
            ),
            detail=f"선택 작업자 {len(selected)}/{setup.required_worker_count}",
        ),
        _coverage_check("required_skills", "필수 기술", set(setup.required_skills), skills),
        _coverage_check(
            "required_certifications",
            "필수 자격",
            set(setup.required_certifications),
            certifications,
        ),
    )


async def _is_current_scope(
    session: AsyncSession,
    setup: DispatchSetup,
) -> tuple[ScopeVersion, bool]:
    version = await session.get(ScopeVersion, setup.source_scope_version_id)
    if version is None or version.job_id != setup.job_id:
        raise DispatchConflictError(setup.source_scope_version_id)
    child_id = await session.scalar(
        select(ScopeVersion.id).where(ScopeVersion.parent_version_id == version.id)
    )
    return version, version.locked_at is not None and child_id is None


async def get_dispatch_view(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
) -> DispatchView:
    job = await _load_job(session, job_id)
    viewer = next(
        (
            stored
            for stored in job.participants
            if stored.id == participant_id and stored.role is ParticipantRole.COMPANY_MANAGER
        ),
        None,
    )
    if viewer is None:
        raise DispatchNotFoundError(job_id)
    setup = await session.scalar(select(DispatchSetup).where(DispatchSetup.job_id == job_id))
    if setup is None:
        return DispatchView(
            job=_job_header(job, viewer),
            setup_id=None,
            dispatch_id=None,
            source_scope_version_id=None,
            source_scope_version_label=None,
            requirements=None,
            vehicle_options=(),
            worker_options=(),
            selected_vehicle_id=None,
            selected_worker_ids=(),
            lead_worker_id=None,
            checks=(),
            worker_note=None,
            status=DispatchStatus.SETUP_REQUIRED,
            confirmed_at=None,
            notification_created=False,
        )
    plan = await session.scalar(select(DispatchPlan).where(DispatchPlan.job_id == job_id))
    version, is_current = await _is_current_scope(session, setup)
    vehicles = _vehicle_options(setup)
    workers = _worker_options(setup)
    return DispatchView(
        job=_job_header(job, viewer),
        setup_id=setup.id,
        dispatch_id=plan.id if plan is not None else None,
        source_scope_version_id=version.id,
        source_scope_version_label=f"v{version.sequence_number}",
        requirements=_requirements(setup),
        vehicle_options=vehicles,
        worker_options=workers,
        selected_vehicle_id=plan.vehicle_option_id if plan is not None else None,
        selected_worker_ids=(
            tuple(UUID(value) for value in plan.selected_worker_option_ids)
            if plan is not None
            else ()
        ),
        lead_worker_id=plan.lead_worker_option_id if plan is not None else None,
        checks=_dispatch_checks(setup, vehicles, workers, plan),
        worker_note=plan.worker_note if plan is not None else None,
        status=(
            DispatchStatus.STALE
            if not is_current
            else DispatchStatus.CONFIRMED
            if plan is not None
            else DispatchStatus.READY
        ),
        confirmed_at=_aware(plan.confirmed_at) if plan is not None else None,
        notification_created=plan is not None,
    )


def _validate_selection(
    setup: DispatchSetup,
    command: DispatchConfirmCreate,
    field_worker_id: UUID,
) -> None:
    vehicles = {option.id: option for option in _vehicle_options(setup)}
    workers = {option.id: option for option in _worker_options(setup)}
    vehicle = vehicles.get(command.vehicle_id)
    selected = tuple(workers.get(worker_id) for worker_id in command.worker_ids)
    if (
        vehicle is None
        or not vehicle.available
        or vehicle.capacity_m2 < setup.required_vehicle_capacity_m2
    ):
        raise DispatchConflictError(command.vehicle_id)
    if len(command.worker_ids) != setup.required_worker_count or any(
        worker is None or not worker.available for worker in selected
    ):
        raise DispatchConflictError(setup.id)
    selected_workers = tuple(worker for worker in selected if worker is not None)
    mapped = tuple(
        worker for worker in selected_workers if worker.participant_id == field_worker_id
    )
    if len(mapped) != 1:
        raise DispatchConflictError(field_worker_id)
    skills = {skill for worker in selected_workers for skill in worker.skills}
    certifications = {
        certification for worker in selected_workers for certification in worker.certifications
    }
    if set(setup.required_skills) - skills or set(setup.required_certifications) - certifications:
        raise DispatchConflictError(setup.id)


async def confirm_dispatch(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
    command: DispatchConfirmCreate,
    *,
    trace_id: str,
) -> DispatchPlan:
    """Atomically validate and freeze one assignment and notification event."""

    command_hash = _command_hash(command)
    job = await _load_job(session, job_id, lock=True)
    existing = await session.scalar(
        select(DispatchPlan).where(DispatchPlan.job_id == job_id).with_for_update()
    )
    if existing is not None:
        if (
            existing.command_hash == command_hash
            and existing.confirmed_by_participant_id == participant_id
        ):
            return existing
        raise DispatchConflictError(job_id)
    _require_open_scheduled_job(job)
    await _require_participant(
        session,
        job_id,
        participant_id,
        ParticipantRole.COMPANY_MANAGER,
    )
    setup = await session.scalar(
        select(DispatchSetup)
        .where(DispatchSetup.id == command.setup_id, DispatchSetup.job_id == job_id)
        .with_for_update()
    )
    if setup is None:
        raise DispatchNotFoundError(command.setup_id)
    await _current_locked_scope(
        session,
        job_id,
        setup.source_scope_version_id,
        lock=True,
    )
    field_worker = next(
        (stored for stored in job.participants if stored.role is ParticipantRole.FIELD_WORKER),
        None,
    )
    if field_worker is None:
        raise DispatchNotFoundError(job_id)
    _validate_selection(setup, command, field_worker.id)
    plan = DispatchPlan(
        job_id=job_id,
        setup_id=setup.id,
        source_scope_version_id=setup.source_scope_version_id,
        vehicle_option_id=command.vehicle_id,
        lead_worker_option_id=command.lead_worker_id,
        selected_worker_option_ids=[str(value) for value in command.worker_ids],
        worker_note=command.worker_note,
        command_hash=command_hash,
        confirmed_by_participant_id=participant_id,
        confirmed_at=utc_now(),
    )
    session.add(plan)
    try:
        await session.flush()
        enqueue_domain_event(
            session,
            DomainEventType.DISPATCH_CONFIRMED_V1,
            job_id,
            actor_id=participant_id,
            trace_id=trace_id,
            payload={
                "dispatch_id": str(plan.id),
                "scope_version_id": str(setup.source_scope_version_id),
                "field_worker_participant_id": str(field_worker.id),
            },
        )
        await session.flush()
    except IntegrityError as error:
        raise DispatchConflictError(job_id) from error
    return plan


def _assigned_worker(
    setup: DispatchSetup,
    plan: DispatchPlan,
    participant_id: UUID,
) -> DispatchWorkerOption:
    selected = {UUID(value) for value in plan.selected_worker_option_ids}
    worker = next(
        (
            option
            for option in _worker_options(setup)
            if option.id in selected and option.participant_id == participant_id
        ),
        None,
    )
    if worker is None:
        raise DispatchNotFoundError(participant_id)
    return worker


async def get_field_brief(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
) -> FieldBriefView:
    job = await _load_job(session, job_id)
    viewer = next(
        (
            stored
            for stored in job.participants
            if stored.id == participant_id and stored.role is ParticipantRole.FIELD_WORKER
        ),
        None,
    )
    if viewer is None:
        raise DispatchNotFoundError(job_id)
    setup = await session.scalar(select(DispatchSetup).where(DispatchSetup.job_id == job_id))
    plan = await session.scalar(select(DispatchPlan).where(DispatchPlan.job_id == job_id))
    if setup is None or plan is None:
        raise DispatchConflictError(job_id)
    version = await _current_locked_scope(session, job_id, setup.source_scope_version_id)
    scope_content = ScopeContent.model_validate(version.content, strict=False)
    quote, agreement_proposal = await _field_brief_quote_context(
        session,
        job_id,
        version.id,
    )
    assert version.locked_at is not None
    _assigned_worker(setup, plan, participant_id)
    workers = {option.id: option for option in _worker_options(setup)}
    vehicles = {option.id: option for option in _vehicle_options(setup)}
    lead = workers.get(plan.lead_worker_option_id)
    vehicle = vehicles.get(plan.vehicle_option_id)
    if lead is None or vehicle is None:
        raise DispatchConflictError(plan.id)
    check_in = await session.scalar(
        select(FieldCheckIn).where(
            FieldCheckIn.job_id == job_id,
            FieldCheckIn.participant_id == participant_id,
        )
    )
    confirmed = set(check_in.confirmed_check_keys) if check_in is not None else set()
    completion_submission = await session.scalar(
        select(CompletionSubmission)
        .where(CompletionSubmission.job_id == job_id)
        .order_by(CompletionSubmission.submitted_at.desc(), CompletionSubmission.id.desc())
        .limit(1)
    )
    completed = (
        set(completion_submission.completed_check_keys)
        if completion_submission is not None
        else set()
    )
    locations = {location.kind: location for location in job.locations}
    origin = locations.get(LocationKind.ORIGIN)
    destination = locations.get(LocationKind.DESTINATION)
    items = tuple(
        FieldBriefCheckItem(
            key=str(item["key"]),
            label=str(item["label"]),
            confirmed=str(item["key"]) in confirmed,
        )
        for item in setup.check_in_items
    )
    completion_items = tuple(
        FieldBriefCheckItem(
            key=str(item["key"]),
            label=str(item["label"]),
            confirmed=str(item["key"]) in completed,
        )
        for item in setup.completion_check_items
    )
    assigned_workers = tuple(
        FieldBriefWorker(
            worker_id=worker_id,
            external_reference=workers[worker_id].external_reference,
            display_name=workers[worker_id].display_name,
            role_label=workers[worker_id].role_label,
            is_lead=worker_id == plan.lead_worker_option_id,
        )
        for worker_id in (UUID(value) for value in plan.selected_worker_option_ids)
        if worker_id in workers
    )
    return FieldBriefView(
        job=_job_header(job, viewer),
        dispatch_id=plan.id,
        scope_version_id=version.id,
        scope_version_label=f"v{version.sequence_number}",
        scope_content_hash=version.content_hash,
        scope_locked_at=_aware(version.locked_at),
        scope_content=scope_content,
        quote=quote,
        included_works=(
            tuple(agreement_proposal.included_works) if agreement_proposal is not None else ()
        ),
        exclusions=(tuple(agreement_proposal.exclusions) if agreement_proposal is not None else ()),
        start_at=_aware(setup.start_at),
        masked_origin=origin.label if origin is not None else None,
        masked_destination=destination.label if destination is not None else None,
        origin_detail_address=(origin.detail_address if origin is not None else None),
        destination_detail_address=(
            destination.detail_address if destination is not None else None
        ),
        lead_worker_name=lead.display_name,
        origin_conditions=tuple(setup.origin_conditions),
        field_check_required_count=sum(not item.confirmed for item in items),
        check_in_items=items,
        completion_check_items=completion_items,
        completion_required_count=sum(not item.confirmed for item in completion_items),
        completion_submission_id=(
            completion_submission.id if completion_submission is not None else None
        ),
        assigned_vehicle=vehicle,
        assigned_worker_count=len(plan.selected_worker_option_ids),
        assigned_workers=assigned_workers,
        required_skills=tuple(setup.required_skills),
        safety_notice=setup.safety_notice,
        checked_in_at=_aware(check_in.checked_in_at) if check_in is not None else None,
    )


def _check_in_response(check_in: FieldCheckIn) -> FieldCheckInResponse:
    return FieldCheckInResponse(
        check_in_id=check_in.id,
        dispatch_id=check_in.dispatch_plan_id,
        participant_id=check_in.participant_id,
        confirmed_check_keys=tuple(check_in.confirmed_check_keys),
        checked_in_at=_aware(check_in.checked_in_at),
    )


async def check_in_field_worker(
    session: AsyncSession,
    job_id: UUID,
    participant_id: UUID,
    command: FieldCheckInCreate,
    *,
    now: datetime | None = None,
) -> FieldCheckInResponse:
    """Record one assigned representative's scheduled-day safety check."""

    job = await _load_job(session, job_id, lock=True)
    _require_open_scheduled_job(job)
    await _require_participant(
        session,
        job_id,
        participant_id,
        ParticipantRole.FIELD_WORKER,
    )
    setup = await session.scalar(
        select(DispatchSetup).where(DispatchSetup.job_id == job_id).with_for_update()
    )
    plan = await session.scalar(
        select(DispatchPlan)
        .where(DispatchPlan.id == command.dispatch_id, DispatchPlan.job_id == job_id)
        .with_for_update()
    )
    if setup is None or plan is None or plan.setup_id != setup.id:
        raise DispatchNotFoundError(command.dispatch_id)
    worker = _assigned_worker(setup, plan, participant_id)
    await _current_locked_scope(session, job_id, setup.source_scope_version_id, lock=True)
    required_keys = tuple(str(item["key"]) for item in setup.check_in_items)
    if set(command.confirmed_check_keys) != set(required_keys):
        raise DispatchConflictError(plan.id)
    existing = await session.scalar(
        select(FieldCheckIn)
        .where(
            FieldCheckIn.job_id == job_id,
            FieldCheckIn.participant_id == participant_id,
        )
        .with_for_update()
    )
    if existing is not None:
        if existing.dispatch_plan_id == plan.id and set(existing.confirmed_check_keys) == set(
            command.confirmed_check_keys
        ):
            return _check_in_response(existing)
        raise DispatchConflictError(job_id)
    checked_at = now or utc_now()
    start_at = _aware(setup.start_at)
    if (
        _aware(checked_at).astimezone(FIELD_SERVICE_TIMEZONE).date()
        != start_at.astimezone(FIELD_SERVICE_TIMEZONE).date()
    ):
        raise DispatchConflictError(start_at)
    check_in = FieldCheckIn(
        job_id=job_id,
        dispatch_plan_id=plan.id,
        participant_id=participant_id,
        worker_option_id=worker.id,
        confirmed_check_keys=list(required_keys),
        checked_in_at=checked_at,
    )
    session.add(check_in)
    try:
        await session.flush()
    except IntegrityError as error:
        raise DispatchConflictError(job_id) from error
    return _check_in_response(check_in)
