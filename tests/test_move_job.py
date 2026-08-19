"""Move job API, validation, and conflict tests."""

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx2 import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import AppEnvironment, Settings
from app.contracts.actor import ActorContext, ActorKind, ParticipantRole
from app.contracts.primitives import JobId, ParticipantId, RequestId
from app.main import create_app
from app.modules.move_job.models import LocationKind, MoveJobStatus
from app.modules.move_job.router import cancel_move_job_endpoint, get_move_job_endpoint
from app.modules.move_job.schemas import (
    CarryDistanceCondition,
    CustomerMoveJobCreate,
    FloorCondition,
    KnowledgeStatus,
    LocationConditions,
    LocationCreate,
    MoveJobCreate,
    ParticipantCreate,
    RoomZoneCreate,
)
from app.modules.move_job.service import (
    MoveJobConflictError,
    MoveJobNotFoundError,
    cancel_move_job,
    get_move_job,
)
from app.platform.db import Base, create_session_factory


@pytest.fixture
async def move_job_client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    database_path = (tmp_path / "move_job.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
    application = create_app(Settings(environment=AppEnvironment.TEST))
    application.state.database_session_factory = create_session_factory(engine)
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
    await engine.dispose()


def _move_job_payload() -> dict[str, object]:
    return {
        "title": "강남 이사",
        "participants": [
            {"role": "customer", "display_name": "고객"},
            {"role": "company_manager", "display_name": "관리자"},
            {"role": "field_worker", "display_name": "현장 담당"},
        ],
        "locations": [
            {
                "kind": "origin",
                "label": "출발지",
                "conditions": {
                    "residence_type": "apartment",
                    "floor": {"status": "known", "value": 12},
                    "elevator": "available",
                    "stairs": "not_required",
                    "parking_access": "restricted",
                    "carry_distance": {"status": "known", "value_m": 35},
                    "access_note": "지하 주차장 높이 확인 필요",
                },
                "room_zones": [
                    {"name": "안방", "sort_order": 1},
                    {"name": "거실", "sort_order": 0},
                ],
            },
            {
                "kind": "destination",
                "label": "도착지",
                "room_zones": [{"name": "거실", "sort_order": 0}],
            },
        ],
    }


def _secret(created: dict[str, Any], role: str) -> str:
    return cast(
        str,
        next(link["secret"] for link in created["access_links"] if link["role"] == role),
    )


@pytest.mark.anyio
async def test_move_job_api_creates_initial_participants_and_reads_job(
    move_job_client: AsyncClient,
) -> None:
    created = await move_job_client.post("/api/v1/move-jobs", json=_move_job_payload())

    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    created_body = created.json()
    job = created_body["job"]
    customer_secret = created_body["access_links"][0]["secret"]
    headers = {"Authorization": f"Bearer {customer_secret}"}
    assert job["status"] == "draft"
    assert [location["kind"] for location in job["locations"]] == [
        "destination",
        "origin",
    ]
    assert [zone["sort_order"] for zone in job["locations"][1]["room_zones"]] == [
        0,
        1,
    ]
    assert job["locations"][0]["conditions"] == {
        "residence_type": "unknown",
        "floor": {"status": "unknown", "value": None},
        "elevator": "unknown",
        "stairs": "unknown",
        "parking_access": "unknown",
        "carry_distance": {"status": "unknown", "value_m": None},
        "access_note": None,
    }
    assert job["locations"][1]["conditions"]["floor"] == {
        "status": "known",
        "value": 12,
    }
    assert job["locations"][1]["conditions"]["carry_distance"] == {
        "status": "known",
        "value_m": 35,
    }

    job_id = job["id"]
    loaded = await move_job_client.get(f"/api/v1/move-jobs/{job_id}", headers=headers)
    assert loaded.status_code == 200
    assert loaded.json() == job
    assert [participant["role"] for participant in job["participants"]] == [
        "company_manager",
        "customer",
        "field_worker",
    ]
    removed_provisioning = await move_job_client.post(
        f"/api/v1/move-jobs/{job_id}/participants",
        json={"role": "company_manager", "display_name": "관리자"},
        headers=headers,
    )
    assert removed_provisioning.status_code == 404


@pytest.mark.anyio
async def test_customer_onboarding_issues_only_the_customer_capability(
    move_job_client: AsyncClient,
) -> None:
    response = await move_job_client.post(
        "/api/v1/move-jobs/onboarding",
        json={
            "title": "소비자 생성 이사",
            "scheduled_at": "2026-08-20T09:00:00+09:00",
            "customer_display_name": "김소비자",
            "locations": [
                {
                    "kind": "origin",
                    "label": "출발지",
                    "room_zones": [{"name": "거실", "sort_order": 0}],
                }
            ],
        },
    )

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"job", "customer_access_link"}
    assert [participant["role"] for participant in body["job"]["participants"]] == ["customer"]
    customer_link = body["customer_access_link"]
    assert customer_link["role"] == "customer"
    assert customer_link["job_id"] == body["job"]["id"]
    loaded = await move_job_client.get(
        f"/api/v1/move-jobs/{body['job']['id']}",
        headers={"Authorization": f"Bearer {customer_link['secret']}"},
    )
    assert loaded.status_code == 200
    assert loaded.json()["participants"] == body["job"]["participants"]
    openapi = (await move_job_client.get("/openapi.json")).json()
    operation = openapi["paths"]["/api/v1/move-jobs/onboarding"]["post"]
    assert "security" not in operation
    response_schema = openapi["components"]["schemas"]["CustomerMoveJobCreatedResponse"]
    assert set(response_schema["properties"]) == {"job", "customer_access_link"}
    request_schema = openapi["components"]["schemas"]["CustomerMoveJobCreate"]
    assert set(request_schema["properties"]) == {
        "title",
        "scheduled_at",
        "customer_display_name",
        "locations",
    }


@pytest.mark.anyio
async def test_only_customer_can_cancel_and_quote_blocks_cancellation(
    move_job_client: AsyncClient,
) -> None:
    created_response = await move_job_client.post(
        "/api/v1/move-jobs",
        json=_move_job_payload(),
    )
    assert created_response.status_code == 201
    created = created_response.json()
    job_id = created["job"]["id"]
    customer_headers = {"Authorization": f"Bearer {_secret(created, 'customer')}"}
    manager_headers = {"Authorization": f"Bearer {_secret(created, 'company_manager')}"}
    delete_url = f"/api/v1/move-jobs/{job_id}"

    assert (await move_job_client.delete(delete_url, headers=manager_headers)).status_code == 403
    zone_id = created["job"]["locations"][0]["room_zones"][0]["id"]
    content = {
        "items": [
            {
                "item_key": "sofa",
                "room_zone_id": zone_id,
                "description": "3인 소파 운반",
            }
        ]
    }
    source = await move_job_client.post(
        f"{delete_url}/scope-versions",
        headers=customer_headers,
        json={"content": content},
    )
    assert source.status_code == 201
    proposal = await move_job_client.post(
        f"{delete_url}/scope-proposals",
        headers=manager_headers,
        json={
            "source_scope_version_id": source.json()["id"],
            "content": content,
            "quote": {
                "base_amount_krw": 500_000,
                "adjustments": [],
                "total_amount_krw": 500_000,
            },
            "execution_plan": {
                "vehicle_count": 1,
                "vehicle_description": "1톤 탑차",
                "worker_count": 2,
                "estimated_duration_minutes": 180,
            },
            "reason": "확정 품목 기준 견적",
        },
    )
    assert proposal.status_code == 201

    blocked = await move_job_client.delete(delete_url, headers=customer_headers)
    assert blocked.status_code == 409
    assert (await move_job_client.get(delete_url, headers=customer_headers)).status_code == 200


def test_customer_onboarding_contract_rejects_duplicate_locations_and_naive_time() -> None:
    payload: dict[str, object] = {
        "title": "이사",
        "customer_display_name": "고객",
        "locations": [
            {
                "kind": "origin",
                "label": "출발지",
                "room_zones": [{"name": "거실", "sort_order": 0}],
            },
            {
                "kind": "origin",
                "label": "다른 출발지",
                "room_zones": [{"name": "안방", "sort_order": 0}],
            },
        ],
    }
    with pytest.raises(ValidationError, match="location kinds must be unique"):
        CustomerMoveJobCreate.model_validate(payload)
    payload["locations"] = [
        {
            "kind": "origin",
            "label": "출발지",
            "room_zones": [{"name": "거실", "sort_order": 0}],
        }
    ]
    payload["scheduled_at"] = "2026-08-20T09:00:00"
    with pytest.raises(ValidationError, match="scheduled_at must include a timezone"):
        CustomerMoveJobCreate.model_validate(payload)


@pytest.mark.anyio
async def test_move_job_api_reports_missing_jobs(move_job_client: AsyncClient) -> None:
    missing_job_id = uuid4()

    loaded = await move_job_client.get(f"/api/v1/move-jobs/{missing_job_id}")
    removed_provisioning = await move_job_client.post(
        f"/api/v1/move-jobs/{missing_job_id}/participants",
        json={"role": "customer", "display_name": "고객"},
    )

    assert loaded.status_code == 401
    assert removed_provisioning.status_code == 404


def test_move_job_command_rejects_duplicate_roles_locations_and_naive_time() -> None:
    participant = ParticipantCreate(role=ParticipantRole.CUSTOMER, display_name="고객")
    participants = (
        participant,
        ParticipantCreate(role=ParticipantRole.COMPANY_MANAGER, display_name="관리자"),
        ParticipantCreate(role=ParticipantRole.FIELD_WORKER, display_name="현장 담당"),
    )
    locations = (
        LocationCreate(
            kind=LocationKind.ORIGIN,
            label="출발지",
            room_zones=(RoomZoneCreate(name="거실", sort_order=0),),
        ),
        LocationCreate(
            kind=LocationKind.ORIGIN,
            label="다른 출발지",
            room_zones=(RoomZoneCreate(name="안방", sort_order=0),),
        ),
    )

    with pytest.raises(ValidationError, match="participant roles must be unique"):
        MoveJobCreate(
            title="이사",
            participants=(participant, participant, participants[2]),
            locations=locations[:1],
        )
    with pytest.raises(ValidationError, match="at least 3 items"):
        MoveJobCreate(title="이사", participants=(participant,), locations=locations[:1])
    with pytest.raises(ValidationError, match="location kinds must be unique"):
        MoveJobCreate(title="이사", participants=participants, locations=locations)
    with pytest.raises(ValidationError, match="scheduled_at must include a timezone"):
        MoveJobCreate(
            title="이사",
            scheduled_at=datetime(2026, 8, 20, 9),
            participants=participants,
            locations=locations[:1],
        )

    aware = MoveJobCreate.model_validate(
        {
            "title": "이사",
            "scheduled_at": "2026-08-20T09:00:00+09:00",
            "participants": [
                {"role": "customer", "display_name": "고객"},
                {"role": "company_manager", "display_name": "관리자"},
                {"role": "field_worker", "display_name": "현장 담당"},
            ],
            "locations": [
                {
                    "kind": "origin",
                    "label": "출발지",
                    "room_zones": [{"name": "거실", "sort_order": 0}],
                }
            ],
        }
    )
    assert aware.scheduled_at is not None
    assert aware.scheduled_at.utcoffset() is not None


@pytest.mark.parametrize(
    "zones, message",
    [
        (
            (
                RoomZoneCreate(name="거실", sort_order=0),
                RoomZoneCreate(name="거실", sort_order=1),
            ),
            "room zone names must be unique",
        ),
        (
            (
                RoomZoneCreate(name="거실", sort_order=0),
                RoomZoneCreate(name="안방", sort_order=0),
            ),
            "room zone sort orders must be unique",
        ),
    ],
)
def test_location_rejects_duplicate_room_zone_identity(
    zones: tuple[RoomZoneCreate, ...],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        LocationCreate(kind=LocationKind.ORIGIN, label="출발지", room_zones=zones)


def test_location_limits_room_zone_expansion() -> None:
    accepted = tuple(RoomZoneCreate(name=f"구역 {index}", sort_order=index) for index in range(100))

    assert (
        len(
            LocationCreate(
                kind=LocationKind.ORIGIN,
                label="출발지",
                room_zones=accepted,
            ).room_zones
        )
        == 100
    )
    with pytest.raises(ValidationError, match="at most 100 items"):
        LocationCreate(
            kind=LocationKind.ORIGIN,
            label="출발지",
            room_zones=(*accepted, RoomZoneCreate(name="초과 구역", sort_order=100)),
        )


def test_location_conditions_require_explicit_numeric_knowledge_state() -> None:
    assert LocationConditions().floor.status is KnowledgeStatus.UNKNOWN
    assert FloorCondition(status=KnowledgeStatus.KNOWN, value=0).value == 0
    assert CarryDistanceCondition(status=KnowledgeStatus.KNOWN, value_m=0).value_m == 0
    with pytest.raises(ValidationError, match="known floor requires a value"):
        FloorCondition(status=KnowledgeStatus.KNOWN)
    with pytest.raises(ValidationError, match="unknown floor forbids one"):
        FloorCondition(value=1)
    with pytest.raises(ValidationError, match="known carry distance requires a value"):
        CarryDistanceCondition(status=KnowledgeStatus.KNOWN)
    with pytest.raises(ValidationError, match="unknown distance forbids one"):
        CarryDistanceCondition(value_m=1)


@pytest.mark.anyio
async def test_move_job_service_and_endpoints_map_disappeared_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    participant_id = uuid4()
    session = AsyncSession()
    actor = ActorContext(
        actor_kind=ActorKind.PARTICIPANT,
        participant_id=ParticipantId(participant_id),
        participant_role=ParticipantRole.CUSTOMER,
        job_id=JobId(job_id),
        request_id=RequestId(uuid4()),
        trace_id="a" * 32,
    )

    async def load_missing(_session: AsyncSession, _job_id: object) -> None:
        return None

    monkeypatch.setattr("app.modules.move_job.service._load_move_job", load_missing)
    with pytest.raises(MoveJobNotFoundError):
        await get_move_job(session, job_id)

    async def raise_missing(*_args: object) -> None:
        raise MoveJobNotFoundError(job_id)

    monkeypatch.setattr("app.modules.move_job.router.get_move_job", raise_missing)
    with pytest.raises(Exception) as get_error:
        await get_move_job_endpoint(job_id, actor, session)
    assert getattr(get_error.value, "status_code", None) == 404

    cancel_session = AsyncMock(spec=AsyncSession)
    cancel_session.scalar = AsyncMock(return_value=None)
    with pytest.raises(MoveJobNotFoundError):
        await cancel_move_job(cancel_session, job_id, participant_id)

    async def raise_cancel_missing(*_args: object) -> None:
        raise MoveJobNotFoundError(job_id)

    async def raise_cancel_conflict(*_args: object) -> None:
        raise MoveJobConflictError(job_id)

    monkeypatch.setattr("app.modules.move_job.router.cancel_move_job", raise_cancel_missing)
    with pytest.raises(Exception) as cancel_missing:
        await cancel_move_job_endpoint(job_id, actor, session)
    assert getattr(cancel_missing.value, "status_code", None) == 404

    monkeypatch.setattr("app.modules.move_job.router.cancel_move_job", raise_cancel_conflict)
    with pytest.raises(Exception) as cancel_conflict:
        await cancel_move_job_endpoint(job_id, actor, session)
    assert getattr(cancel_conflict.value, "status_code", None) == 409
    await session.close()


@pytest.mark.anyio
async def test_cancel_move_job_service_handles_terminal_states_and_existing_quote() -> None:
    session = AsyncMock(spec=AsyncSession)
    actor_id = uuid4()
    job_id = uuid4()

    session.scalar = AsyncMock(return_value=SimpleNamespace(status=MoveJobStatus.CANCELED))
    await cancel_move_job(session, job_id, actor_id)

    session.scalar = AsyncMock(return_value=SimpleNamespace(status=MoveJobStatus.COMPLETED))
    with pytest.raises(MoveJobConflictError):
        await cancel_move_job(session, job_id, actor_id)

    session.scalar = AsyncMock(side_effect=[SimpleNamespace(status=MoveJobStatus.DRAFT), uuid4()])
    with pytest.raises(MoveJobConflictError):
        await cancel_move_job(session, job_id, actor_id)
