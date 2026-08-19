"""A-13 dispatch, field brief, and representative check-in tests."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from httpx2 import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import AppEnvironment, Settings
from app.contracts.actor import ParticipantRole
from app.contracts.events import DomainEventType
from app.main import create_app
from app.modules.dispatch import router as dispatch_router
from app.modules.dispatch import service as dispatch_service
from app.modules.dispatch.models import DispatchPlan, DispatchSetup, FieldCheckIn
from app.modules.dispatch.schemas import (
    DispatchConfirmCreate,
    DispatchSetupCreate,
    DispatchVehicleCreate,
    DispatchWorkerCreate,
    FieldCheckInCreate,
)
from app.modules.dispatch.service import (
    DispatchConflictError,
    DispatchNotFoundError,
    check_in_field_worker,
    get_dispatch_view,
    get_field_brief,
)
from app.modules.field_change.models import ChangeProposalDetail, FieldIssue, FieldIssueType
from app.modules.move_job.models import JobParticipant, MoveJob, MoveJobStatus
from app.modules.notification.models import NotificationDelivery
from app.modules.notification.service import consume_notification_event
from app.modules.scope.models import ChangeRequest, ChangeRequestStatus, ScopeVersion
from app.modules.scope_review.models import ScopeProposal, ScopeProposalKind, ScopeProposalStatus
from app.platform.db import Base, create_session_factory
from app.platform.event_bus.models import OutboxEvent
from app.platform.event_bus.service import _to_domain_event

DispatchApi = tuple[AsyncClient, async_sessionmaker[AsyncSession]]


@pytest.fixture
async def dispatch_api(tmp_path: Path) -> AsyncIterator[DispatchApi]:
    database_path = (tmp_path / "dispatch.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        poolclass=NullPool,
    )
    factory = create_session_factory(engine)
    application = create_app(Settings(environment=AppEnvironment.TEST))
    application.state.database_session_factory = factory
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        yield client, factory
    await engine.dispose()


async def _create_job(
    client: AsyncClient,
    *,
    scheduled_at: datetime | None = None,
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/move-jobs",
        json={
            "title": "A-13 합성 배차",
            "scheduled_at": (
                scheduled_at or datetime.now(UTC).replace(hour=8, minute=0, second=0)
            ).isoformat(),
            "participants": [
                {"role": "customer", "display_name": "합성 고객"},
                {"role": "company_manager", "display_name": "합성 업체"},
                {"role": "field_worker", "display_name": "합성 기사"},
            ],
            "locations": [
                {
                    "kind": "origin",
                    "label": "서울 출발지(마스킹)",
                    "detail_address": "101동 1203호",
                    "room_zones": [{"name": "거실", "sort_order": 0}],
                },
                {
                    "kind": "destination",
                    "label": "인천 도착지(마스킹)",
                    "detail_address": "B동 502호",
                    "room_zones": [{"name": "거실", "sort_order": 0}],
                },
            ],
        },
    )
    assert response.status_code == 201
    return cast(dict[str, Any], response.json())


def _participant_id(created: dict[str, Any], role: str) -> UUID:
    return UUID(
        next(
            participant["id"]
            for participant in created["job"]["participants"]
            if participant["role"] == role
        )
    )


def _headers(created: dict[str, Any], role: str) -> dict[str, str]:
    secret = next(link["secret"] for link in created["access_links"] if link["role"] == role)
    return {"Authorization": f"Bearer {secret}"}


async def _locked_scope(
    factory: async_sessionmaker[AsyncSession],
    created: dict[str, Any],
    *,
    locked: bool = True,
    parent_id: UUID | None = None,
    sequence_number: int = 1,
    with_quote: bool = False,
) -> UUID:
    scope_id = uuid4()
    origin = next(
        location for location in created["job"]["locations"] if location["kind"] == "origin"
    )
    content = {
        "schema_version": 1,
        "items": [
            {
                "item_key": "bed",
                "room_zone_id": origin["room_zones"][0]["id"],
                "description": "침대 포장과 운반",
            }
        ],
    }
    async with factory.begin() as session:
        if with_quote:
            source_id = uuid4()
            session.add(
                ScopeVersion(
                    id=source_id,
                    job_id=UUID(created["job"]["id"]),
                    parent_version_id=None,
                    sequence_number=1,
                    content=content,
                    content_hash="1" * 64,
                    created_by_participant_id=_participant_id(created, "customer"),
                    created_at=datetime.now(UTC),
                )
            )
            parent_id = source_id
            sequence_number = 2
        session.add(
            ScopeVersion(
                id=scope_id,
                job_id=UUID(created["job"]["id"]),
                parent_version_id=parent_id,
                sequence_number=sequence_number,
                content=content,
                content_hash=f"{sequence_number:x}" * 64,
                created_by_participant_id=_participant_id(created, "company_manager"),
                created_at=datetime.now(UTC),
                locked_at=datetime.now(UTC) if locked else None,
            )
        )
        if with_quote:
            session.add(
                ScopeProposal(
                    job_id=UUID(created["job"]["id"]),
                    source_scope_version_id=parent_id,
                    result_scope_version_id=scope_id,
                    proposed_by_participant_id=_participant_id(created, "company_manager"),
                    kind=ScopeProposalKind.INITIAL,
                    status=ScopeProposalStatus.CONFIRMED,
                    base_amount_krw=500_000,
                    adjustments=[{"label": "사다리차", "amount_krw": 100_000}],
                    total_amount_krw=600_000,
                    included_works=["포장", "운반"],
                    exclusions=["에어컨 설치"],
                    reason="확정 견적",
                    sent_at=datetime.now(UTC),
                    confirmed_at=datetime.now(UTC),
                )
            )
    return scope_id


async def _approved_change_scope(
    factory: async_sessionmaker[AsyncSession],
    created: dict[str, Any],
    base_scope_id: UUID,
) -> UUID:
    result_scope_id = uuid4()
    field_issue_id = uuid4()
    change_request_id = uuid4()
    now = datetime.now(UTC)
    async with factory.begin() as session:
        base = await session.get(ScopeVersion, base_scope_id)
        assert base is not None
        session.add(
            ScopeVersion(
                id=result_scope_id,
                job_id=base.job_id,
                parent_version_id=base.id,
                sequence_number=base.sequence_number + 1,
                content=base.content,
                content_hash="3" * 64,
                created_by_participant_id=_participant_id(created, "company_manager"),
                created_at=now,
                locked_at=now,
            )
        )
        session.add(
            FieldIssue(
                id=field_issue_id,
                job_id=base.job_id,
                client_reference=uuid4(),
                base_scope_version_id=base.id,
                reported_by_participant_id=_participant_id(created, "company_manager"),
                issue_type=FieldIssueType.SITE_BLOCKER,
                title="사다리차 반입 필요",
                description="현장 진입 조건 변경",
                created_at=now,
            )
        )
        session.add(
            ChangeRequest(
                id=change_request_id,
                job_id=base.job_id,
                base_scope_version_id=base.id,
                requested_by_participant_id=_participant_id(created, "company_manager"),
                description="사다리차 반입 추가",
                proposed_content=base.content,
                status=ChangeRequestStatus.APPROVED,
                decided_by_participant_id=_participant_id(created, "customer"),
                decision_note="증빙 확인",
                decided_at=now,
                result_scope_version_id=result_scope_id,
                created_at=now,
            )
        )
        session.add(
            ChangeProposalDetail(
                change_request_id=change_request_id,
                field_issue_id=field_issue_id,
                title="사다리차 반입 추가",
                base_amount_krw=600_000,
                adjustments=[{"label": "사다리차", "amount_krw": 150_000}],
                total_amount_krw=750_000,
                created_at=now,
            )
        )
    return result_scope_id


def _setup_payload(created: dict[str, Any], scope_id: UUID) -> dict[str, Any]:
    return {
        "client_reference": str(uuid4()),
        "source_scope_version_id": str(scope_id),
        "expected_duration_minutes": 180,
        "required_vehicle_capacity_m2": 25,
        "required_worker_count": 2,
        "required_skills": ["대형가구", "포장"],
        "required_certifications": ["화물운송"],
        "check_in_items": [
            {"key": "identity", "label": "배정 기사 확인"},
            {"key": "safety", "label": "안전 장비 확인"},
        ],
        "origin_conditions": ["엘리베이터 사용 가능", "주차 20분"],
        "safety_notice": "보호 장갑을 착용하고 고객 확인 전 운반을 시작하지 않습니다",
        "vehicles": [
            {
                "external_reference": "vehicle-ready",
                "display_name": "1톤 윙바디 12가3456",
                "specification": "적재 30m2",
                "equipment": ["리프트"],
                "capacity_m2": 30,
                "available": True,
            },
            {
                "external_reference": "vehicle-small",
                "display_name": "소형 밴 34나5678",
                "specification": "적재 10m2",
                "equipment": [],
                "capacity_m2": 10,
                "available": True,
            },
            {
                "external_reference": "vehicle-busy",
                "display_name": "1톤 탑차 56다7890",
                "specification": "적재 35m2",
                "equipment": [],
                "capacity_m2": 35,
                "available": False,
                "conflict_reason": "같은 시간 다른 배차",
            },
        ],
        "workers": [
            {
                "external_reference": "worker-representative",
                "display_name": "합성 기사",
                "role_label": "현장 리더",
                "skills": ["대형가구"],
                "certifications": ["화물운송"],
                "available": True,
                "participant_id": str(_participant_id(created, "field_worker")),
            },
            {
                "external_reference": "worker-helper",
                "display_name": "합성 보조",
                "role_label": "포장 보조",
                "skills": ["포장"],
                "certifications": [],
                "available": True,
            },
            {
                "external_reference": "worker-busy",
                "display_name": "합성 비가용",
                "role_label": "포장 보조",
                "skills": ["포장"],
                "certifications": [],
                "available": False,
                "conflict_reason": "휴무",
            },
            {
                "external_reference": "worker-unskilled",
                "display_name": "합성 일반 보조",
                "role_label": "일반 보조",
                "skills": [],
                "certifications": [],
                "available": True,
            },
        ],
    }


def _selection(view: dict[str, Any]) -> dict[str, Any]:
    vehicle_id = next(
        item["id"]
        for item in view["vehicle_options"]
        if item["external_reference"] == "vehicle-ready"
    )
    workers = {item["external_reference"]: item["id"] for item in view["worker_options"]}
    return {
        "setup_id": view["setup_id"],
        "vehicle_id": vehicle_id,
        "lead_worker_id": workers["worker-representative"],
        "worker_ids": [workers["worker-representative"], workers["worker-helper"]],
        "worker_note": "출발 30분 전에 고객에게 상태만 확인",
    }


def test_field_brief_rejects_unconfirmed_quote_snapshot() -> None:
    assert dispatch_service._confirmed_quote(None) is None
    proposal = ScopeProposal(
        job_id=uuid4(),
        source_scope_version_id=uuid4(),
        result_scope_version_id=uuid4(),
        proposed_by_participant_id=uuid4(),
        kind=ScopeProposalKind.INITIAL,
        status=ScopeProposalStatus.CUSTOMER_REVIEW,
        base_amount_krw=100_000,
        adjustments=[],
        total_amount_krw=100_000,
        included_works=[],
        exclusions=[],
        reason="검토 중",
        sent_at=datetime.now(UTC),
    )
    with pytest.raises(DispatchConflictError):
        dispatch_service._confirmed_quote(proposal)


@pytest.mark.anyio
async def test_field_brief_quote_context_rejects_ambiguous_sources() -> None:
    session = AsyncMock(spec=AsyncSession)
    session.scalar.return_value = SimpleNamespace()
    rows = SimpleNamespace(one_or_none=lambda: (SimpleNamespace(), SimpleNamespace()))
    session.execute.return_value = SimpleNamespace(tuples=lambda: rows)

    with pytest.raises(DispatchConflictError):
        await dispatch_service._field_brief_quote_context(session, uuid4(), uuid4())


@pytest.mark.anyio
async def test_field_brief_quote_context_rejects_change_without_agreement() -> None:
    session = AsyncMock(spec=AsyncSession)
    session.scalar.side_effect = (None, None)
    rows = SimpleNamespace(one_or_none=lambda: (SimpleNamespace(), SimpleNamespace()))
    session.execute.return_value = SimpleNamespace(tuples=lambda: rows)

    with pytest.raises(DispatchConflictError):
        await dispatch_service._field_brief_quote_context(session, uuid4(), uuid4())


async def _setup(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    created: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    scope_id = await _locked_scope(factory, created)
    payload = _setup_payload(created, scope_id)
    response = await client.post(
        f"/api/v1/move-jobs/{created['job']['id']}/dispatch/setup",
        headers=_headers(created, "company_manager"),
        json=payload,
    )
    assert response.status_code == 201
    return cast(dict[str, Any], response.json()), payload


@pytest.mark.anyio
async def test_dispatch_to_field_check_in_is_replay_safe_and_notifies_worker(
    dispatch_api: DispatchApi,
) -> None:
    client, factory = dispatch_api
    created = await _create_job(client)
    job_id = created["job"]["id"]
    manager_headers = _headers(created, "company_manager")
    worker_headers = _headers(created, "field_worker")

    unauthorized = await client.get(f"/api/v1/move-jobs/{job_id}/dispatch")
    assert unauthorized.status_code == 401
    forbidden = await client.get(
        f"/api/v1/move-jobs/{job_id}/dispatch",
        headers=_headers(created, "customer"),
    )
    assert forbidden.status_code == 403
    empty = await client.get(
        f"/api/v1/move-jobs/{job_id}/dispatch",
        headers=manager_headers,
    )
    assert empty.status_code == 200
    assert empty.headers["cache-control"] == "no-store"
    assert empty.json()["status"] == "setup_required"

    scope_id = await _locked_scope(factory, created, with_quote=True)
    setup_payload = _setup_payload(created, scope_id)
    setup = await client.post(
        f"/api/v1/move-jobs/{job_id}/dispatch/setup",
        headers=manager_headers,
        json=setup_payload,
    )
    assert setup.status_code == 201
    assert setup.headers["cache-control"] == "no-store"
    setup_view = cast(dict[str, Any], setup.json())
    assert setup_view["status"] == "ready"
    assert [check["status"] for check in setup_view["checks"]] == [
        "pass",
        "pass",
        "pass",
        "pass",
    ]
    assert setup_view["requirements"]["start_at"]
    assert setup_view["requirements"]["required_vehicle_count"] == 1

    replay = await client.post(
        f"/api/v1/move-jobs/{job_id}/dispatch/setup",
        headers=manager_headers,
        json=setup_payload,
    )
    assert replay.status_code == 201
    assert replay.json()["setup_id"] == setup_view["setup_id"]
    assert replay.json()["vehicle_options"] == setup_view["vehicle_options"]
    conflict_payload = {**setup_payload, "client_reference": str(uuid4())}
    conflict = await client.post(
        f"/api/v1/move-jobs/{job_id}/dispatch/setup",
        headers=manager_headers,
        json=conflict_payload,
    )
    assert conflict.status_code == 409

    selection = _selection(setup_view)
    confirmed = await client.put(
        f"/api/v1/move-jobs/{job_id}/dispatch",
        headers=manager_headers,
        json=selection,
    )
    assert confirmed.status_code == 200
    assert confirmed.headers["cache-control"] == "no-store"
    confirmed_view = cast(dict[str, Any], confirmed.json())
    assert confirmed_view["status"] == "confirmed"
    assert confirmed_view["notification_created"] is True
    assert confirmed_view["worker_note"] == selection["worker_note"]
    assert confirmed_view["selected_worker_ids"] == selection["worker_ids"]

    confirmed_replay = await client.put(
        f"/api/v1/move-jobs/{job_id}/dispatch",
        headers=manager_headers,
        json=selection,
    )
    assert confirmed_replay.status_code == 200
    changed = await client.put(
        f"/api/v1/move-jobs/{job_id}/dispatch",
        headers=manager_headers,
        json={**selection, "worker_note": "다른 메모"},
    )
    assert changed.status_code == 409

    async with factory.begin() as session:
        events = (
            await session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == DomainEventType.DISPATCH_CONFIRMED_V1
                )
            )
        ).all()
        assert len(events) == 1
        event = _to_domain_event(events[0])
        assert event.payload == {
            "dispatch_id": confirmed_view["dispatch_id"],
            "scope_version_id": str(scope_id),
            "field_worker_participant_id": str(_participant_id(created, "field_worker")),
        }
        intents = await consume_notification_event(session, event)
        assert len(intents) == 1
        assert intents[0].recipient_participant_id == _participant_id(created, "field_worker")
        assert intents[0].event_type is DomainEventType.DISPATCH_CONFIRMED_V1
        assert await consume_notification_event(session, event) == ()

    brief = await client.get(
        f"/api/v1/move-jobs/{job_id}/field-brief",
        headers=worker_headers,
    )
    assert brief.status_code == 200
    assert brief.headers["cache-control"] == "no-store"
    brief_body = brief.json()
    assert brief_body["dispatch_id"] == confirmed_view["dispatch_id"]
    assert brief_body["scope_version_id"] == str(scope_id)
    assert brief_body["scope_content_hash"] == "2" * 64
    assert brief_body["scope_locked_at"] is not None
    assert brief_body["scope_content"]["items"][0]["item_key"] == "bed"
    assert brief_body["quote"]["total_amount_krw"] == 600_000
    assert brief_body["included_works"] == ["포장", "운반"]
    assert brief_body["exclusions"] == ["에어컨 설치"]
    assert brief_body["masked_origin"] == "서울 출발지(마스킹)"
    assert brief_body["masked_destination"] == "인천 도착지(마스킹)"
    assert brief_body["origin_detail_address"] == "101동 1203호"
    assert brief_body["destination_detail_address"] == "B동 502호"
    assert brief_body["lead_worker_call_uri"] is None
    assert brief_body["company_chat_uri"] is None
    assert brief_body["navigation_uri"] is None
    assert brief_body["field_check_required_count"] == 2
    assert not any(item["confirmed"] for item in brief_body["check_in_items"])

    incomplete = await client.post(
        f"/api/v1/move-jobs/{job_id}/check-ins",
        headers=worker_headers,
        json={
            "dispatch_id": confirmed_view["dispatch_id"],
            "confirmed_check_keys": ["identity"],
        },
    )
    assert incomplete.status_code == 409
    check_in_payload = {
        "dispatch_id": confirmed_view["dispatch_id"],
        "confirmed_check_keys": ["identity", "safety"],
    }
    check_in = await client.post(
        f"/api/v1/move-jobs/{job_id}/check-ins",
        headers=worker_headers,
        json=check_in_payload,
    )
    assert check_in.status_code == 201
    first_checked_at = check_in.json()["checked_in_at"]
    check_in_replay = await client.post(
        f"/api/v1/move-jobs/{job_id}/check-ins",
        headers=worker_headers,
        json=check_in_payload,
    )
    assert check_in_replay.status_code == 201
    assert check_in_replay.json()["checked_in_at"] == first_checked_at
    after = await client.get(
        f"/api/v1/move-jobs/{job_id}/field-brief",
        headers=worker_headers,
    )
    assert after.json()["field_check_required_count"] == 0
    assert all(item["confirmed"] for item in after.json()["check_in_items"])

    manager_forbidden = await client.get(
        f"/api/v1/move-jobs/{job_id}/field-brief",
        headers=manager_headers,
    )
    assert manager_forbidden.status_code == 403
    worker_forbidden = await client.put(
        f"/api/v1/move-jobs/{job_id}/dispatch",
        headers=worker_headers,
        json=selection,
    )
    assert worker_forbidden.status_code == 403


@pytest.mark.anyio
async def test_field_brief_uses_approved_change_quote_and_inherited_classifications(
    dispatch_api: DispatchApi,
) -> None:
    client, factory = dispatch_api
    created = await _create_job(client)
    base_scope_id = await _locked_scope(factory, created, with_quote=True)
    changed_scope_id = await _approved_change_scope(factory, created, base_scope_id)
    manager_headers = _headers(created, "company_manager")

    setup = await client.post(
        f"/api/v1/move-jobs/{created['job']['id']}/dispatch/setup",
        headers=manager_headers,
        json=_setup_payload(created, changed_scope_id),
    )
    assert setup.status_code == 201
    selection = _selection(cast(dict[str, Any], setup.json()))
    confirmed = await client.put(
        f"/api/v1/move-jobs/{created['job']['id']}/dispatch",
        headers=manager_headers,
        json=selection,
    )
    assert confirmed.status_code == 200

    brief = await client.get(
        f"/api/v1/move-jobs/{created['job']['id']}/field-brief",
        headers=_headers(created, "field_worker"),
    )
    assert brief.status_code == 200
    assert brief.json()["scope_version_id"] == str(changed_scope_id)
    assert brief.json()["quote"] == {
        "base_amount_krw": 600_000,
        "adjustments": [{"label": "사다리차", "amount_krw": 150_000}],
        "total_amount_krw": 750_000,
    }
    assert brief.json()["included_works"] == ["포장", "운반"]
    assert brief.json()["exclusions"] == ["에어컨 설치"]


@pytest.mark.anyio
async def test_dispatch_rejects_invalid_snapshot_and_candidate_selections(
    dispatch_api: DispatchApi,
) -> None:
    client, factory = dispatch_api
    created = await _create_job(client)
    job_id = created["job"]["id"]
    manager_headers = _headers(created, "company_manager")
    scope_id = await _locked_scope(factory, created)
    payload = _setup_payload(created, scope_id)

    wrong_mapping = {
        **payload,
        "workers": [
            {**worker, "participant_id": None}
            for worker in cast(list[dict[str, Any]], payload["workers"])
        ],
    }
    response = await client.post(
        f"/api/v1/move-jobs/{job_id}/dispatch/setup",
        headers=manager_headers,
        json=wrong_mapping,
    )
    assert response.status_code == 409

    too_many = {**payload, "required_worker_count": 5}
    response = await client.post(
        f"/api/v1/move-jobs/{job_id}/dispatch/setup",
        headers=manager_headers,
        json=too_many,
    )
    assert response.status_code == 409

    setup, _ = await _setup(client, factory, await _create_job(client))
    other_created = await _create_job(client)
    other_setup, _ = await _setup(client, factory, other_created)
    other_job_id = other_created["job"]["id"]
    options = {item["external_reference"]: item["id"] for item in other_setup["worker_options"]}
    vehicles = {item["external_reference"]: item["id"] for item in other_setup["vehicle_options"]}
    base = _selection(other_setup)
    invalid_commands = (
        {**base, "vehicle_id": str(uuid4())},
        {**base, "vehicle_id": vehicles["vehicle-busy"]},
        {**base, "vehicle_id": vehicles["vehicle-small"]},
        {
            **base,
            "worker_ids": [options["worker-representative"]],
            "lead_worker_id": options["worker-representative"],
        },
        {**base, "worker_ids": [options["worker-representative"], options["worker-busy"]]},
        {
            **base,
            "worker_ids": [options["worker-helper"], options["worker-unskilled"]],
            "lead_worker_id": options["worker-helper"],
        },
        {**base, "worker_ids": [options["worker-representative"], options["worker-unskilled"]]},
        {**base, "setup_id": setup["setup_id"]},
    )
    for command in invalid_commands:
        response = await client.put(
            f"/api/v1/move-jobs/{other_job_id}/dispatch",
            headers=_headers(other_created, "company_manager"),
            json=command,
        )
        assert response.status_code in {404, 409}


@pytest.mark.anyio
async def test_dispatch_stale_scope_missing_state_and_scheduled_day_guards(
    dispatch_api: DispatchApi,
) -> None:
    client, factory = dispatch_api
    created = await _create_job(client)
    setup, _ = await _setup(client, factory, created)
    job_id = UUID(created["job"]["id"])
    manager_id = _participant_id(created, "company_manager")
    worker_id = _participant_id(created, "field_worker")

    async with factory.begin() as session:
        source_id = UUID(setup["source_scope_version_id"])
        session.add(
            ScopeVersion(
                job_id=job_id,
                parent_version_id=source_id,
                sequence_number=2,
                content={"schema_version": 1, "items": []},
                content_hash="b" * 64,
                created_by_participant_id=manager_id,
                created_at=datetime.now(UTC),
                locked_at=datetime.now(UTC),
            )
        )
    stale = await client.get(
        f"/api/v1/move-jobs/{job_id}/dispatch",
        headers=_headers(created, "company_manager"),
    )
    assert stale.status_code == 200
    assert stale.json()["status"] == "stale"
    stale_confirm = await client.put(
        f"/api/v1/move-jobs/{job_id}/dispatch",
        headers=_headers(created, "company_manager"),
        json=_selection(setup),
    )
    assert stale_confirm.status_code == 409
    missing_brief = await client.get(
        f"/api/v1/move-jobs/{job_id}/field-brief",
        headers=_headers(created, "field_worker"),
    )
    assert missing_brief.status_code == 409

    fresh = await _create_job(client)
    fresh_setup, _ = await _setup(client, factory, fresh)
    fresh_job_id = fresh["job"]["id"]
    selection = _selection(fresh_setup)
    confirmed = await client.put(
        f"/api/v1/move-jobs/{fresh_job_id}/dispatch",
        headers=_headers(fresh, "company_manager"),
        json=selection,
    )
    assert confirmed.status_code == 200
    command = FieldCheckInCreate(
        dispatch_id=UUID(confirmed.json()["dispatch_id"]),
        confirmed_check_keys=("identity", "safety"),
    )
    async with factory.begin() as session:
        with pytest.raises(DispatchConflictError):
            await check_in_field_worker(
                session,
                UUID(fresh_job_id),
                _participant_id(fresh, "field_worker"),
                command,
                now=datetime.now(UTC) + timedelta(days=1),
            )
        with pytest.raises(DispatchNotFoundError):
            await get_field_brief(session, uuid4(), worker_id)
        with pytest.raises(DispatchNotFoundError):
            await get_dispatch_view(session, UUID(fresh_job_id), worker_id)


def test_dispatch_request_models_reject_ambiguous_snapshots() -> None:
    with pytest.raises(ValidationError):
        DispatchVehicleCreate(
            external_reference="v",
            display_name="차량",
            specification="1톤",
            capacity_m2=1,
            available=False,
        )
    with pytest.raises(ValidationError):
        DispatchVehicleCreate(
            external_reference="v",
            display_name="차량",
            specification="1톤",
            capacity_m2=1,
            available=True,
            conflict_reason="충돌",
        )
    with pytest.raises(ValidationError):
        DispatchWorkerCreate(
            external_reference="w",
            display_name="기사",
            role_label="리더",
            available=False,
        )
    with pytest.raises(ValidationError):
        DispatchWorkerCreate(
            external_reference="w",
            display_name="기사",
            role_label="리더",
            available=True,
            conflict_reason="휴무",
        )

    base = {
        "client_reference": uuid4(),
        "source_scope_version_id": uuid4(),
        "expected_duration_minutes": 10,
        "required_vehicle_capacity_m2": 1,
        "required_worker_count": 1,
        "check_in_items": [{"key": "same", "label": "A"}, {"key": "same", "label": "B"}],
        "safety_notice": "안전",
        "vehicles": [
            {
                "external_reference": "same",
                "display_name": "차량",
                "specification": "1톤",
                "capacity_m2": 1,
                "available": True,
            }
        ],
        "workers": [
            {
                "external_reference": "worker",
                "display_name": "기사",
                "role_label": "리더",
                "available": True,
            }
        ],
    }
    with pytest.raises(ValidationError, match="unique"):
        DispatchSetupCreate.model_validate(base)
    worker_id = uuid4()
    duplicate_participants = {
        **base,
        "check_in_items": [{"key": "first", "label": "A"}],
        "workers": [
            {**cast(list[dict[str, Any]], base["workers"])[0], "participant_id": worker_id},
            {
                **cast(list[dict[str, Any]], base["workers"])[0],
                "external_reference": "worker-2",
                "participant_id": worker_id,
            },
        ],
    }
    with pytest.raises(ValidationError, match="participant IDs"):
        DispatchSetupCreate.model_validate(duplicate_participants)
    with pytest.raises(ValidationError, match="unique"):
        DispatchConfirmCreate(
            setup_id=uuid4(),
            vehicle_id=uuid4(),
            lead_worker_id=worker_id,
            worker_ids=(worker_id, worker_id),
        )
    with pytest.raises(ValidationError, match="lead worker"):
        DispatchConfirmCreate(
            setup_id=uuid4(),
            vehicle_id=uuid4(),
            lead_worker_id=uuid4(),
            worker_ids=(worker_id,),
        )
    with pytest.raises(ValidationError, match="unique"):
        FieldCheckInCreate(
            dispatch_id=uuid4(),
            confirmed_check_keys=("identity", "identity"),
        )


@pytest.mark.anyio
async def test_dispatch_closed_or_unscheduled_job_and_corrupt_plan_are_rejected(
    dispatch_api: DispatchApi,
) -> None:
    client, factory = dispatch_api
    created = await _create_job(client)
    setup, _ = await _setup(client, factory, created)
    selection = _selection(setup)
    job_id = UUID(created["job"]["id"])
    manager_id = _participant_id(created, "company_manager")

    async with factory.begin() as session:
        job = await session.get(MoveJob, job_id)
        assert job is not None
        job.status = MoveJobStatus.CANCELED
    closed = await client.put(
        f"/api/v1/move-jobs/{job_id}/dispatch",
        headers=_headers(created, "company_manager"),
        json=selection,
    )
    assert closed.status_code == 409

    unscheduled = await _create_job(client)
    unscheduled_id = UUID(unscheduled["job"]["id"])
    scope_id = await _locked_scope(factory, unscheduled)
    async with factory.begin() as session:
        job = await session.get(MoveJob, unscheduled_id)
        assert job is not None
        job.scheduled_at = None
    no_schedule = await client.post(
        f"/api/v1/move-jobs/{unscheduled_id}/dispatch/setup",
        headers=_headers(unscheduled, "company_manager"),
        json=_setup_payload(unscheduled, scope_id),
    )
    assert no_schedule.status_code == 409

    corrupt = await _create_job(client)
    corrupt_setup, _ = await _setup(client, factory, corrupt)
    confirmed = await client.put(
        f"/api/v1/move-jobs/{corrupt['job']['id']}/dispatch",
        headers=_headers(corrupt, "company_manager"),
        json=_selection(corrupt_setup),
    )
    assert confirmed.status_code == 200
    async with factory.begin() as session:
        plan = await session.get(DispatchPlan, UUID(confirmed.json()["dispatch_id"]))
        assert plan is not None
        plan.lead_worker_option_id = uuid4()
    corrupt_brief = await client.get(
        f"/api/v1/move-jobs/{corrupt['job']['id']}/field-brief",
        headers=_headers(corrupt, "field_worker"),
    )
    assert corrupt_brief.status_code == 409

    async with factory.begin() as session:
        with pytest.raises(DispatchNotFoundError):
            await get_dispatch_view(session, uuid4(), manager_id)
        assert await session.scalar(select(DispatchSetup).where(DispatchSetup.job_id == job_id))
        assert await session.scalar(select(NotificationDelivery)) is None
        assert await session.scalar(select(FieldCheckIn)) is None


@pytest.mark.anyio
async def test_dispatch_missing_scope_participant_assignment_and_worker_are_rejected(
    dispatch_api: DispatchApi,
) -> None:
    client, factory = dispatch_api
    created = await _create_job(client)
    job_id = created["job"]["id"]
    manager_headers = _headers(created, "company_manager")
    payload = _setup_payload(created, uuid4())
    missing_scope = await client.post(
        f"/api/v1/move-jobs/{job_id}/dispatch/setup",
        headers=manager_headers,
        json=payload,
    )
    assert missing_scope.status_code == 404

    unlocked_scope = await _locked_scope(factory, created, locked=False)
    unlocked = await client.post(
        f"/api/v1/move-jobs/{job_id}/dispatch/setup",
        headers=manager_headers,
        json=_setup_payload(created, unlocked_scope),
    )
    assert unlocked.status_code == 409

    no_worker = await _create_job(client)
    no_worker_scope = await _locked_scope(factory, no_worker)
    async with factory.begin() as session:
        worker = await session.get(
            JobParticipant,
            _participant_id(no_worker, "field_worker"),
        )
        assert worker is not None
        await session.delete(worker)
    setup_without_worker = await client.post(
        f"/api/v1/move-jobs/{no_worker['job']['id']}/dispatch/setup",
        headers=_headers(no_worker, "company_manager"),
        json=_setup_payload(no_worker, no_worker_scope),
    )
    assert setup_without_worker.status_code == 404

    deleted_after_setup = await _create_job(client)
    setup_view, _ = await _setup(client, factory, deleted_after_setup)
    async with factory.begin() as session:
        worker = await session.get(
            JobParticipant,
            _participant_id(deleted_after_setup, "field_worker"),
        )
        assert worker is not None
        await session.delete(worker)
    confirm_without_worker = await client.put(
        f"/api/v1/move-jobs/{deleted_after_setup['job']['id']}/dispatch",
        headers=_headers(deleted_after_setup, "company_manager"),
        json=_selection(setup_view),
    )
    assert confirm_without_worker.status_code == 404

    confirmed_created = await _create_job(client)
    confirmed_setup, _ = await _setup(client, factory, confirmed_created)
    confirmation = await client.put(
        f"/api/v1/move-jobs/{confirmed_created['job']['id']}/dispatch",
        headers=_headers(confirmed_created, "company_manager"),
        json=_selection(confirmed_setup),
    )
    assert confirmation.status_code == 200
    wrong_dispatch = await client.post(
        f"/api/v1/move-jobs/{confirmed_created['job']['id']}/check-ins",
        headers=_headers(confirmed_created, "field_worker"),
        json={
            "dispatch_id": str(uuid4()),
            "confirmed_check_keys": ["identity", "safety"],
        },
    )
    assert wrong_dispatch.status_code == 404

    async with factory.begin() as session:
        with pytest.raises(DispatchNotFoundError):
            await get_field_brief(
                session,
                UUID(confirmed_created["job"]["id"]),
                _participant_id(confirmed_created, "company_manager"),
            )
        plan = await session.get(DispatchPlan, UUID(confirmation.json()["dispatch_id"]))
        setup = await session.get(DispatchSetup, UUID(confirmed_setup["setup_id"]))
        assert plan is not None and setup is not None
        plan.selected_worker_option_ids = [str(uuid4())]
        with pytest.raises(DispatchNotFoundError):
            dispatch_service._assigned_worker(
                setup,
                plan,
                _participant_id(confirmed_created, "field_worker"),
            )


@pytest.mark.anyio
async def test_dispatch_defensive_integrity_branches(
    dispatch_api: DispatchApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory = dispatch_api
    created = await _create_job(client)
    setup_view, payload = await _setup(client, factory, created)
    job_id = UUID(created["job"]["id"])
    manager_id = _participant_id(created, "company_manager")
    worker_id = _participant_id(created, "field_worker")
    setup_id = UUID(setup_view["setup_id"])
    async with factory() as real_session:
        setup = await real_session.get(DispatchSetup, setup_id)
        assert setup is not None
        real_session.expunge(setup)

    session = AsyncMock(spec=AsyncSession)
    session.scalar = AsyncMock(return_value=None)
    with pytest.raises(DispatchNotFoundError):
        await dispatch_service._require_participant(
            session,
            job_id,
            manager_id,
            ParticipantRole.COMPANY_MANAGER,
        )
    with pytest.raises(DispatchNotFoundError):
        await dispatch_service._current_locked_scope(session, job_id, uuid4())
    unlocked = SimpleNamespace(locked_at=None)
    session.scalar = AsyncMock(return_value=unlocked)
    with pytest.raises(DispatchConflictError):
        await dispatch_service._current_locked_scope(session, job_id, uuid4())

    session.get = AsyncMock(return_value=None)
    with pytest.raises(DispatchConflictError):
        await dispatch_service._is_current_scope(session, setup)

    job = SimpleNamespace(
        id=job_id,
        status=MoveJobStatus.DRAFT,
        scheduled_at=datetime.now(UTC),
        participants=[SimpleNamespace(id=worker_id, role=ParticipantRole.FIELD_WORKER)],
    )
    monkeypatch.setattr(dispatch_service, "_load_job", AsyncMock(return_value=job))
    monkeypatch.setattr(dispatch_service, "_require_participant", AsyncMock())
    monkeypatch.setattr(dispatch_service, "_current_locked_scope", AsyncMock())
    command = DispatchSetupCreate.model_validate(payload)
    session.scalar = AsyncMock(return_value=None)
    session.flush = AsyncMock(side_effect=IntegrityError("insert", {}, Exception("race")))
    with pytest.raises(DispatchConflictError):
        await dispatch_service.create_dispatch_setup(
            session,
            job_id,
            manager_id,
            command,
        )

    selection = DispatchConfirmCreate.model_validate(_selection(setup_view))
    monkeypatch.setattr(dispatch_service, "_validate_selection", lambda *args: None)
    session.scalar = AsyncMock(side_effect=[None, setup])
    session.flush = AsyncMock(side_effect=IntegrityError("insert", {}, Exception("race")))
    with pytest.raises(DispatchConflictError):
        await dispatch_service.confirm_dispatch(
            session,
            job_id,
            manager_id,
            selection,
            trace_id="0" * 32,
        )

    plan = DispatchPlan(
        id=uuid4(),
        job_id=job_id,
        setup_id=setup.id,
        source_scope_version_id=setup.source_scope_version_id,
        vehicle_option_id=selection.vehicle_id,
        lead_worker_option_id=selection.lead_worker_id,
        selected_worker_option_ids=[str(value) for value in selection.worker_ids],
        command_hash="a" * 64,
        confirmed_by_participant_id=manager_id,
        confirmed_at=datetime.now(UTC),
    )
    check_command = FieldCheckInCreate(
        dispatch_id=plan.id,
        confirmed_check_keys=("identity", "safety"),
    )
    monkeypatch.setattr(
        dispatch_service,
        "_assigned_worker",
        lambda *args: SimpleNamespace(id=selection.lead_worker_id),
    )
    existing = FieldCheckIn(
        id=uuid4(),
        job_id=job_id,
        dispatch_plan_id=uuid4(),
        participant_id=worker_id,
        worker_option_id=selection.lead_worker_id,
        confirmed_check_keys=["identity", "safety"],
        checked_in_at=datetime.now(UTC),
    )
    session.scalar = AsyncMock(side_effect=[setup, plan, existing])
    with pytest.raises(DispatchConflictError):
        await check_in_field_worker(
            session,
            job_id,
            worker_id,
            check_command,
        )

    session.scalar = AsyncMock(side_effect=[setup, plan, None])
    session.flush = AsyncMock(side_effect=IntegrityError("insert", {}, Exception("race")))
    with pytest.raises(DispatchConflictError):
        await check_in_field_worker(
            session,
            job_id,
            worker_id,
            check_command,
            now=datetime.now(UTC),
        )


@pytest.mark.anyio
async def test_dispatch_router_maps_service_not_found_and_conflict_errors(
    dispatch_api: DispatchApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = dispatch_api
    created = await _create_job(client)
    job_id = created["job"]["id"]
    manager_headers = _headers(created, "company_manager")
    worker_headers = _headers(created, "field_worker")

    monkeypatch.setattr(
        dispatch_router,
        "get_dispatch_view",
        AsyncMock(side_effect=DispatchNotFoundError(job_id)),
    )
    not_found = await client.get(
        f"/api/v1/move-jobs/{job_id}/dispatch",
        headers=manager_headers,
    )
    assert not_found.status_code == 404
    monkeypatch.setattr(
        dispatch_router,
        "get_dispatch_view",
        AsyncMock(side_effect=DispatchConflictError(job_id)),
    )
    conflict = await client.get(
        f"/api/v1/move-jobs/{job_id}/dispatch",
        headers=manager_headers,
    )
    assert conflict.status_code == 409
    monkeypatch.setattr(
        dispatch_router,
        "get_field_brief",
        AsyncMock(side_effect=DispatchNotFoundError(job_id)),
    )
    brief = await client.get(
        f"/api/v1/move-jobs/{job_id}/field-brief",
        headers=worker_headers,
    )
    assert brief.status_code == 404
    monkeypatch.setattr(
        dispatch_router,
        "check_in_field_worker",
        AsyncMock(side_effect=DispatchNotFoundError(job_id)),
    )
    check_in = await client.post(
        f"/api/v1/move-jobs/{job_id}/check-ins",
        headers=worker_headers,
        json={
            "dispatch_id": str(uuid4()),
            "confirmed_check_keys": ["identity"],
        },
    )
    assert check_in.status_code == 404
