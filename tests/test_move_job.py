"""Move job API, validation, and conflict tests."""

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from httpx2 import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import AppEnvironment, Settings
from app.contracts.actor import ParticipantRole
from app.main import create_app
from app.modules.move_job.models import LocationKind, MoveJob
from app.modules.move_job.schemas import (
    LocationCreate,
    MoveJobCreate,
    ParticipantCreate,
    RoomZoneCreate,
)
from app.modules.move_job.service import ParticipantRoleConflictError, connect_participant
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
        "participants": [{"role": "customer", "display_name": "고객"}],
        "locations": [
            {
                "kind": "origin",
                "label": "출발지",
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


@pytest.mark.anyio
async def test_move_job_api_creates_reads_and_connects_participant(
    move_job_client: AsyncClient,
) -> None:
    created = await move_job_client.post("/api/v1/move-jobs", json=_move_job_payload())

    assert created.status_code == 201
    created_body = created.json()
    assert created_body["status"] == "draft"
    assert [location["kind"] for location in created_body["locations"]] == [
        "destination",
        "origin",
    ]
    assert [zone["sort_order"] for zone in created_body["locations"][1]["room_zones"]] == [
        0,
        1,
    ]

    job_id = created_body["id"]
    loaded = await move_job_client.get(f"/api/v1/move-jobs/{job_id}")
    assert loaded.status_code == 200
    assert loaded.json() == created_body

    connected = await move_job_client.post(
        f"/api/v1/move-jobs/{job_id}/participants",
        json={"role": "field_worker", "display_name": "현장 담당"},
    )
    assert connected.status_code == 201
    assert [participant["role"] for participant in connected.json()["participants"]] == [
        "customer",
        "field_worker",
    ]

    conflict = await move_job_client.post(
        f"/api/v1/move-jobs/{job_id}/participants",
        json={"role": "customer", "display_name": "다른 고객"},
    )
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "participant role already exists"}


@pytest.mark.anyio
async def test_move_job_api_reports_missing_jobs(move_job_client: AsyncClient) -> None:
    missing_job_id = uuid4()

    loaded = await move_job_client.get(f"/api/v1/move-jobs/{missing_job_id}")
    connected = await move_job_client.post(
        f"/api/v1/move-jobs/{missing_job_id}/participants",
        json={"role": "customer", "display_name": "고객"},
    )

    assert loaded.status_code == 404
    assert connected.status_code == 404


def test_move_job_command_rejects_duplicate_roles_locations_and_naive_time() -> None:
    participant = ParticipantCreate(role=ParticipantRole.CUSTOMER, display_name="고객")
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
            participants=(participant, participant),
            locations=locations[:1],
        )
    with pytest.raises(ValidationError, match="location kinds must be unique"):
        MoveJobCreate(title="이사", participants=(participant,), locations=locations)
    with pytest.raises(ValidationError, match="scheduled_at must include a timezone"):
        MoveJobCreate(
            title="이사",
            scheduled_at=datetime(2026, 8, 20, 9),
            participants=(participant,),
            locations=locations[:1],
        )

    aware = MoveJobCreate.model_validate(
        {
            "title": "이사",
            "scheduled_at": "2026-08-20T09:00:00+09:00",
            "participants": [{"role": "customer", "display_name": "고객"}],
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


@pytest.mark.anyio
async def test_participant_connection_maps_database_race_to_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = MoveJob(id=uuid4(), title="이사")
    job.participants = []
    job.locations = []
    session = AsyncSession()

    async def load_job(_session: AsyncSession, _job_id: object) -> MoveJob:
        return job

    async def fail_flush() -> None:
        raise IntegrityError("duplicate role", {}, RuntimeError("duplicate"))

    monkeypatch.setattr("app.modules.move_job.service._load_move_job", load_job)
    monkeypatch.setattr(session, "flush", fail_flush)

    with pytest.raises(ParticipantRoleConflictError):
        await connect_participant(
            session,
            job.id,
            ParticipantCreate(role=ParticipantRole.CUSTOMER, display_name="고객"),
        )
    await session.close()
