"""Access-link secrecy, expiry, revocation, and role policy tests."""

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import AppEnvironment, Settings
from app.main import create_app
from app.modules.access.models import ParticipantAccessToken
from app.platform.db import Base, create_session_factory


@pytest.fixture
async def access_api(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncClient, async_sessionmaker[AsyncSession]]]:
    database_path = (tmp_path / "access.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
    factory = create_session_factory(engine)
    application = create_app(Settings(environment=AppEnvironment.TEST))
    application.state.database_session_factory = factory
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client, factory
    await engine.dispose()


async def _create_job(client: AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/move-jobs",
        json={
            "title": "권한 테스트",
            "participants": [
                {"role": "customer", "display_name": "고객"},
                {"role": "company_manager", "display_name": "관리자"},
            ],
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
    return cast(dict[str, Any], response.json())


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


@pytest.mark.anyio
async def test_access_links_store_only_hash_and_enforce_job_and_role_boundaries(
    access_api: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, factory = access_api
    first = await _create_job(client)
    second = await _create_job(client)
    job = first["job"]
    links = first["access_links"]
    customer_link, manager_link = links
    customer_secret = customer_link["secret"]
    manager_secret = manager_link["secret"]

    async with factory() as session:
        stored_hashes = set(
            (await session.scalars(select(ParticipantAccessToken.token_hash))).all()
        )
    assert customer_secret not in stored_hashes
    assert hashlib.sha256(customer_secret.encode()).hexdigest() in stored_hashes

    assert (await client.get(f"/api/v1/move-jobs/{job['id']}")).status_code == 401
    invalid = await client.get(
        f"/api/v1/move-jobs/{job['id']}",
        headers=_bearer("not-a-token"),
    )
    assert invalid.status_code == 401
    assert invalid.headers["www-authenticate"] == "Bearer"

    cross_job = await client.get(
        f"/api/v1/move-jobs/{second['job']['id']}",
        headers=_bearer(customer_secret),
    )
    assert cross_job.status_code == 404

    connected = await client.post(
        f"/api/v1/move-jobs/{job['id']}/participants",
        json={"role": "field_worker", "display_name": "현장 담당"},
        headers=_bearer(manager_secret),
    )
    assert connected.status_code == 201
    worker_secret = connected.json()["access_link"]["secret"]
    denied = await client.post(
        f"/api/v1/move-jobs/{job['id']}/participants",
        json={"role": "customer", "display_name": "다른 고객"},
        headers=_bearer(worker_secret),
    )
    assert denied.status_code == 403


@pytest.mark.anyio
async def test_access_link_rotation_expiry_and_revocation(
    access_api: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, factory = access_api
    created = await _create_job(client)
    job_id = created["job"]["id"]
    customer_link, manager_link = created["access_links"]
    customer_id = customer_link["participant_id"]
    old_secret = customer_link["secret"]
    manager_secret = manager_link["secret"]

    rotated = await client.post(
        f"/api/v1/move-jobs/{job_id}/participants/{customer_id}/access-links",
        headers=_bearer(manager_secret),
    )
    assert rotated.status_code == 201
    new_link = rotated.json()
    new_secret = new_link["secret"]
    assert new_secret != old_secret
    assert (
        await client.get(f"/api/v1/move-jobs/{job_id}", headers=_bearer(old_secret))
    ).status_code == 401
    assert (
        await client.get(f"/api/v1/move-jobs/{job_id}", headers=_bearer(new_secret))
    ).status_code == 200

    forbidden = await client.post(
        f"/api/v1/move-jobs/{job_id}/participants/{manager_link['participant_id']}/access-links",
        headers=_bearer(new_secret),
    )
    assert forbidden.status_code == 403
    forbidden_revoke = await client.post(
        f"/api/v1/move-jobs/{job_id}/access-links/{manager_link['id']}/revoke",
        headers=_bearer(new_secret),
    )
    assert forbidden_revoke.status_code == 403

    revoked = await client.post(
        f"/api/v1/move-jobs/{job_id}/access-links/{new_link['id']}/revoke",
        headers=_bearer(manager_secret),
    )
    assert revoked.status_code == 204
    repeated = await client.post(
        f"/api/v1/move-jobs/{job_id}/access-links/{new_link['id']}/revoke",
        headers=_bearer(manager_secret),
    )
    assert repeated.status_code == 204
    assert (
        await client.get(f"/api/v1/move-jobs/{job_id}", headers=_bearer(new_secret))
    ).status_code == 401

    async with factory.begin() as session:
        now = datetime.now(UTC)
        await session.execute(
            update(ParticipantAccessToken)
            .where(ParticipantAccessToken.id == UUID(manager_link["id"]))
            .values(
                created_at=now - timedelta(days=2),
                expires_at=now - timedelta(days=1),
            )
        )
    assert (
        await client.get(f"/api/v1/move-jobs/{job_id}", headers=_bearer(manager_secret))
    ).status_code == 401


@pytest.mark.anyio
async def test_access_link_endpoints_hide_missing_targets_and_validate_expiry(
    access_api: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, _ = access_api
    created = await _create_job(client)
    job_id = created["job"]["id"]
    manager_secret = created["access_links"][1]["secret"]

    missing_participant = await client.post(
        f"/api/v1/move-jobs/{job_id}/participants/{uuid4()}/access-links",
        headers=_bearer(manager_secret),
    )
    missing_link = await client.post(
        f"/api/v1/move-jobs/{job_id}/access-links/{uuid4()}/revoke",
        headers=_bearer(manager_secret),
    )
    assert missing_participant.status_code == 404
    assert missing_link.status_code == 404
