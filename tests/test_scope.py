"""Immutable scope version, hash, authorization, and conflict tests."""

import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from httpx2 import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import AppEnvironment, Settings
from app.contracts.actor import ParticipantRole
from app.contracts.primitives import utc_now
from app.main import create_app
from app.modules.scope.models import ScopeApproval, ScopeVersion
from app.modules.scope.schemas import ScopeContent, ScopeItem, ScopeVersionCreate
from app.modules.scope.service import (
    ScopeApprovalConflictError,
    ScopeResourceNotFoundError,
    ScopeVersionConflictError,
    approve_scope_version,
    create_scope_version,
    list_scope_versions,
)
from app.platform.db import Base, create_session_factory


@pytest.fixture
async def scope_client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    database_path = (tmp_path / "scope.sqlite3").as_posix()
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


async def _create_job(client: AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/move-jobs",
        json={
            "title": "작업범위 테스트",
            "participants": [
                {"role": "customer", "display_name": "고객"},
                {"role": "company_manager", "display_name": "관리자"},
                {"role": "field_worker", "display_name": "현장 담당"},
            ],
            "locations": [
                {
                    "kind": "origin",
                    "label": "출발지",
                    "room_zones": [
                        {"name": "거실", "sort_order": 0},
                        {"name": "안방", "sort_order": 1},
                    ],
                }
            ],
        },
    )
    assert response.status_code == 201
    return cast(dict[str, Any], response.json())


def _secret(created: dict[str, Any], role: str) -> str:
    return cast(
        str,
        next(link["secret"] for link in created["access_links"] if link["role"] == role),
    )


def _headers(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _scope_payload(created: dict[str, Any], *, reverse: bool = False) -> dict[str, Any]:
    zones = created["job"]["locations"][0]["room_zones"]
    items = [
        {
            "item_key": "bed",
            "room_zone_id": zones[1]["id"],
            "description": "침대 분해와 운반",
        },
        {
            "item_key": "sofa",
            "room_zone_id": zones[0]["id"],
            "description": "소파 운반",
        },
    ]
    return {"content": {"schema_version": 1, "items": list(reversed(items)) if reverse else items}}


@pytest.mark.anyio
async def test_scope_api_creates_linear_immutable_history_with_stable_hash(
    scope_client: AsyncClient,
) -> None:
    created = await _create_job(scope_client)
    job_id = created["job"]["id"]
    customer_secret = _secret(created, "customer")
    manager_secret = _secret(created, "company_manager")
    url = f"/api/v1/move-jobs/{job_id}/scope-versions"

    first = await scope_client.post(
        url,
        headers=_headers(customer_secret),
        json=_scope_payload(created, reverse=True),
    )
    assert first.status_code == 201
    first_body = first.json()
    assert first_body["sequence_number"] == 1
    assert first_body["parent_version_id"] is None
    assert [item["item_key"] for item in first_body["content"]["items"]] == ["bed", "sofa"]
    canonical = json.dumps(
        first_body["content"],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert first_body["content_hash"] == hashlib.sha256(canonical.encode()).hexdigest()

    second_payload = _scope_payload(created)
    second_payload["parent_version_id"] = first_body["id"]
    second_payload["content"]["items"][1]["description"] = "소파 포장과 운반"
    second = await scope_client.post(
        url,
        headers=_headers(manager_secret),
        json=second_payload,
    )
    assert second.status_code == 201
    second_body = second.json()
    assert second_body["sequence_number"] == 2
    assert second_body["parent_version_id"] == first_body["id"]
    assert second_body["content_hash"] != first_body["content_hash"]

    versions = await scope_client.get(url, headers=_headers(customer_secret))
    assert versions.status_code == 200
    assert [version["sequence_number"] for version in versions.json()] == [1, 2]
    assert versions.json()[0] == first_body
    assert versions.json()[1] == second_body


@pytest.mark.anyio
async def test_scope_api_rejects_stale_parent_cross_job_zone_and_role(
    scope_client: AsyncClient,
) -> None:
    first_job = await _create_job(scope_client)
    second_job = await _create_job(scope_client)
    job_id = first_job["job"]["id"]
    url = f"/api/v1/move-jobs/{job_id}/scope-versions"
    customer_secret = _secret(first_job, "customer")
    worker_secret = _secret(first_job, "field_worker")

    assert (await scope_client.post(url, json=_scope_payload(first_job))).status_code == 401
    denied = await scope_client.post(
        url,
        headers=_headers(worker_secret),
        json=_scope_payload(first_job),
    )
    assert denied.status_code == 403

    first = await scope_client.post(
        url,
        headers=_headers(customer_secret),
        json=_scope_payload(first_job),
    )
    assert first.status_code == 201
    duplicate_root = await scope_client.post(
        url,
        headers=_headers(customer_secret),
        json=_scope_payload(first_job),
    )
    assert duplicate_root.status_code == 409

    child_payload = _scope_payload(first_job)
    child_payload["parent_version_id"] = first.json()["id"]
    child = await scope_client.post(
        url,
        headers=_headers(customer_secret),
        json=child_payload,
    )
    assert child.status_code == 201
    worker_history = await scope_client.get(
        url,
        headers=_headers(_secret(first_job, "field_worker")),
    )
    assert worker_history.status_code == 403
    stale = await scope_client.post(
        url,
        headers=_headers(customer_secret),
        json=child_payload,
    )
    assert stale.status_code == 409

    foreign_zone_payload = _scope_payload(first_job)
    foreign_zone_payload["parent_version_id"] = child.json()["id"]
    foreign_zone_payload["content"]["items"][0]["room_zone_id"] = second_job["job"]["locations"][0][
        "room_zones"
    ][0]["id"]
    foreign_zone = await scope_client.post(
        url,
        headers=_headers(customer_secret),
        json=foreign_zone_payload,
    )
    assert foreign_zone.status_code == 404

    cross_job_parent = _scope_payload(second_job)
    cross_job_parent["parent_version_id"] = child.json()["id"]
    cross_job = await scope_client.post(
        f"/api/v1/move-jobs/{second_job['job']['id']}/scope-versions",
        headers=_headers(_secret(second_job, "customer")),
        json=cross_job_parent,
    )
    assert cross_job.status_code == 404
    hidden_list = await scope_client.get(
        f"/api/v1/move-jobs/{second_job['job']['id']}/scope-versions",
        headers=_headers(customer_secret),
    )
    assert hidden_list.status_code == 404


def test_scope_content_rejects_duplicate_keys_and_invalid_shape() -> None:
    zone_id = uuid4()
    item = ScopeItem(item_key="sofa", room_zone_id=zone_id, description="소파 운반")

    with pytest.raises(ValidationError, match="scope item keys must be unique"):
        ScopeContent(items=(item, item))
    with pytest.raises(ValidationError):
        ScopeVersionCreate.model_validate({"content": {"items": []}})
    with pytest.raises(ValidationError):
        ScopeVersionCreate.model_validate(
            {
                "content": {
                    "items": [{"item_key": "", "room_zone_id": str(zone_id), "description": "운반"}]
                }
            }
        )


@pytest.mark.anyio
async def test_scope_service_maps_missing_actor_and_database_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncSession()
    job_id = uuid4()
    command = ScopeVersionCreate(
        content=ScopeContent(
            items=(ScopeItem(item_key="sofa", room_zone_id=uuid4(), description="소파 운반"),)
        )
    )

    async def missing_participant(_statement: object) -> None:
        return None

    monkeypatch.setattr(session, "scalar", missing_participant)
    with pytest.raises(ScopeResourceNotFoundError):
        await create_scope_version(session, job_id, uuid4(), command)

    participant_id = uuid4()
    zone_id = command.content.items[0].room_zone_id
    scalar_results = iter([participant_id, None])

    async def scalar(_statement: object) -> object:
        return next(scalar_results)

    class ZoneResult:
        def all(self) -> tuple[UUID, ...]:
            return (zone_id,)

    async def scalars(_statement: object) -> ZoneResult:
        return ZoneResult()

    async def fail_flush() -> None:
        raise IntegrityError("duplicate parent", {}, RuntimeError("duplicate"))

    monkeypatch.setattr(session, "scalar", scalar)
    monkeypatch.setattr(session, "scalars", scalars)
    monkeypatch.setattr(session, "flush", fail_flush)
    with pytest.raises(ScopeVersionConflictError):
        await create_scope_version(session, job_id, participant_id, command)
    await session.close()


@pytest.mark.anyio
async def test_scope_approvals_lock_same_version_and_block_further_edit(
    scope_client: AsyncClient,
) -> None:
    created = await _create_job(scope_client)
    job_id = created["job"]["id"]
    customer_secret = _secret(created, "customer")
    manager_secret = _secret(created, "company_manager")
    versions_url = f"/api/v1/move-jobs/{job_id}/scope-versions"
    version = await scope_client.post(
        versions_url,
        headers=_headers(customer_secret),
        json=_scope_payload(created),
    )
    assert version.status_code == 201
    version_id = version.json()["id"]
    approval_url = f"{versions_url}/{version_id}/approvals"

    customer_approval = await scope_client.post(
        approval_url,
        headers=_headers(customer_secret),
    )
    assert customer_approval.status_code == 201
    assert customer_approval.json()["approval"]["role"] == "customer"
    assert customer_approval.json()["version"]["approval_roles"] == ["customer"]
    assert customer_approval.json()["version"]["locked_at"] is None

    duplicate = await scope_client.post(
        approval_url,
        headers=_headers(customer_secret),
    )
    assert duplicate.status_code == 409
    manager_approval = await scope_client.post(
        approval_url,
        headers=_headers(manager_secret),
    )
    assert manager_approval.status_code == 201
    locked = manager_approval.json()["version"]
    assert locked["approval_roles"] == ["customer", "company_manager"]
    assert locked["locked_at"] == manager_approval.json()["approval"]["approved_at"]

    after_lock = _scope_payload(created)
    after_lock["parent_version_id"] = version_id
    edit = await scope_client.post(
        versions_url,
        headers=_headers(customer_secret),
        json=after_lock,
    )
    assert edit.status_code == 409
    repeated_manager = await scope_client.post(
        approval_url,
        headers=_headers(manager_secret),
    )
    assert repeated_manager.status_code == 409
    history = await scope_client.get(versions_url, headers=_headers(customer_secret))
    assert history.json()[0]["locked_at"] is not None
    assert history.json()[0]["approval_roles"] == ["customer", "company_manager"]


@pytest.mark.anyio
async def test_scope_approvals_do_not_combine_across_versions(
    scope_client: AsyncClient,
) -> None:
    created = await _create_job(scope_client)
    job_id = created["job"]["id"]
    customer_secret = _secret(created, "customer")
    manager_secret = _secret(created, "company_manager")
    versions_url = f"/api/v1/move-jobs/{job_id}/scope-versions"
    first = await scope_client.post(
        versions_url,
        headers=_headers(customer_secret),
        json=_scope_payload(created),
    )
    first_approval_url = f"{versions_url}/{first.json()['id']}/approvals"
    assert (
        await scope_client.post(first_approval_url, headers=_headers(customer_secret))
    ).status_code == 201

    second_payload = _scope_payload(created)
    second_payload["parent_version_id"] = first.json()["id"]
    second = await scope_client.post(
        versions_url,
        headers=_headers(manager_secret),
        json=second_payload,
    )
    second_approval_url = f"{versions_url}/{second.json()['id']}/approvals"
    manager_approval = await scope_client.post(
        second_approval_url,
        headers=_headers(manager_secret),
    )

    assert manager_approval.status_code == 201
    assert manager_approval.json()["version"]["approval_roles"] == ["company_manager"]
    assert manager_approval.json()["version"]["locked_at"] is None

    customer_approval = await scope_client.post(
        second_approval_url,
        headers=_headers(customer_secret),
    )
    assert customer_approval.status_code == 201
    assert customer_approval.json()["version"]["approval_roles"] == [
        "customer",
        "company_manager",
    ]
    assert customer_approval.json()["version"]["locked_at"] is not None


@pytest.mark.anyio
async def test_scope_approval_rejects_past_version_role_and_cross_job(
    scope_client: AsyncClient,
) -> None:
    first_job = await _create_job(scope_client)
    second_job = await _create_job(scope_client)
    job_id = first_job["job"]["id"]
    customer_secret = _secret(first_job, "customer")
    manager_secret = _secret(first_job, "company_manager")
    worker_secret = _secret(first_job, "field_worker")
    versions_url = f"/api/v1/move-jobs/{job_id}/scope-versions"
    first = await scope_client.post(
        versions_url,
        headers=_headers(customer_secret),
        json=_scope_payload(first_job),
    )
    second_payload = _scope_payload(first_job)
    second_payload["parent_version_id"] = first.json()["id"]
    second = await scope_client.post(
        versions_url,
        headers=_headers(manager_secret),
        json=second_payload,
    )
    first_approval_url = f"{versions_url}/{first.json()['id']}/approvals"

    past = await scope_client.post(first_approval_url, headers=_headers(customer_secret))
    denied = await scope_client.post(
        f"{versions_url}/{second.json()['id']}/approvals",
        headers=_headers(worker_secret),
    )
    cross_job = await scope_client.post(
        f"/api/v1/move-jobs/{second_job['job']['id']}/scope-versions/"
        f"{second.json()['id']}/approvals",
        headers=_headers(_secret(second_job, "customer")),
    )
    missing = await scope_client.post(
        f"{versions_url}/{uuid4()}/approvals",
        headers=_headers(customer_secret),
    )

    assert past.status_code == 409
    assert denied.status_code == 403
    assert cross_job.status_code == 404
    assert missing.status_code == 404


@pytest.mark.anyio
async def test_scope_approval_service_maps_missing_actor_and_database_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncSession()
    job_id = uuid4()
    version_id = uuid4()

    async def missing_participant(_statement: object) -> None:
        return None

    monkeypatch.setattr(session, "scalar", missing_participant)
    with pytest.raises(ScopeResourceNotFoundError):
        await approve_scope_version(
            session,
            job_id,
            version_id,
            uuid4(),
            ParticipantRole.CUSTOMER,
        )

    participant_id = uuid4()
    version = ScopeVersion(
        id=version_id,
        job_id=job_id,
        sequence_number=1,
        content={
            "schema_version": 1,
            "items": [
                {
                    "item_key": "sofa",
                    "room_zone_id": str(uuid4()),
                    "description": "소파 운반",
                }
            ],
        },
        content_hash="a" * 64,
        created_by_participant_id=participant_id,
    )
    version.created_at = utc_now()
    scalar_results = iter([participant_id, version, None])

    async def scalar(_statement: object) -> object:
        return next(scalar_results)

    class EmptyRoles:
        def all(self) -> tuple[ParticipantRole, ...]:
            return ()

    async def scalars(_statement: object) -> EmptyRoles:
        return EmptyRoles()

    async def fail_flush() -> None:
        raise IntegrityError("duplicate approval", {}, RuntimeError("duplicate"))

    monkeypatch.setattr(session, "scalar", scalar)
    monkeypatch.setattr(session, "scalars", scalars)
    monkeypatch.setattr(session, "flush", fail_flush)
    with pytest.raises(ScopeApprovalConflictError):
        await approve_scope_version(
            session,
            job_id,
            version_id,
            participant_id,
            ParticipantRole.CUSTOMER,
        )
    await session.close()


@pytest.mark.anyio
async def test_scope_version_list_uses_two_selects_for_many_versions(tmp_path: Path) -> None:
    database_path = (tmp_path / "scope-query-count.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
    factory = create_session_factory(engine)
    job_id = uuid4()
    participant_id = uuid4()
    room_zone_id = uuid4()
    version_ids = [uuid4() for _ in range(3)]
    versions = [
        ScopeVersion(
            id=version_ids[index - 1],
            job_id=job_id,
            parent_version_id=version_ids[index - 2] if index > 1 else None,
            sequence_number=index,
            content={
                "schema_version": 1,
                "items": [
                    {
                        "item_key": f"item-{index}",
                        "room_zone_id": str(room_zone_id),
                        "description": f"item {index}",
                    }
                ],
            },
            content_hash=str(index).zfill(64),
            created_by_participant_id=participant_id,
        )
        for index in range(1, 4)
    ]
    approvals = [
        ScopeApproval(
            scope_version_id=version_ids[0],
            participant_id=uuid4(),
            role=ParticipantRole.CUSTOMER,
        ),
        ScopeApproval(
            scope_version_id=version_ids[1],
            participant_id=uuid4(),
            role=ParticipantRole.COMPANY_MANAGER,
        ),
    ]
    async with factory.begin() as session:
        session.add_all([*versions, *approvals])

    selects = 0

    def count_selects(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        nonlocal selects
        selects += statement.lstrip().upper().startswith("SELECT")

    event.listen(engine.sync_engine, "before_cursor_execute", count_selects)
    try:
        async with factory() as session:
            empty_responses = await list_scope_versions(session, uuid4())
            empty_selects = selects
            selects = 0
            responses = await list_scope_versions(session, job_id)
            populated_selects = selects
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count_selects)
        await engine.dispose()

    assert empty_responses == ()
    assert empty_selects == 1
    assert [response.sequence_number for response in responses] == [1, 2, 3]
    assert [response.approval_roles for response in responses] == [
        (ParticipantRole.CUSTOMER,),
        (ParticipantRole.COMPANY_MANAGER,),
        (),
    ]
    assert populated_selects == 2
