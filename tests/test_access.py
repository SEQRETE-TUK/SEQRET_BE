"""Access-link secrecy, expiry, revocation, and role policy tests."""

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.config import AppEnvironment, Settings
from app.contracts.fakes import FakeCache
from app.contracts.ports import ProviderError, ProviderErrorKind
from app.main import create_app
from app.modules.access.models import ParticipantAccessToken
from app.modules.access.service import (
    InvalidAccessTokenError,
    _increment_database_rate_window,
    rotate_access_link,
)
from app.modules.move_job.models import JobParticipant
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


async def _access_api_with_settings(
    tmp_path: Path,
    settings: Settings,
) -> tuple[
    AsyncClient,
    async_sessionmaker[AsyncSession],
    tuple[FastAPI, AsyncEngine],
]:
    database_path = (tmp_path / f"access-{uuid4().hex}.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
    factory = create_session_factory(engine)
    application = create_app(settings)
    application.state.database_session_factory = factory
    client = AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    )
    return client, factory, (application, engine)


async def _create_job(client: AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/move-jobs",
        json={
            "title": "권한 테스트",
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
        },
    )
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
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
    customer_link, manager_link, _ = links
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

    removed_provisioning = await client.post(
        f"/api/v1/move-jobs/{job['id']}/participants",
        json={"role": "field_worker", "display_name": "현장 담당"},
        headers=_bearer(manager_secret),
    )
    cross_role_rotation = await client.post(
        f"/api/v1/move-jobs/{job['id']}/participants/{customer_link['participant_id']}/access-links",
        headers=_bearer(manager_secret),
    )
    cross_job_link = await client.post(
        f"/api/v1/move-jobs/{job['id']}/access-links/{second['access_links'][0]['id']}/revoke",
        headers=_bearer(manager_secret),
    )
    cross_job_path = await client.post(
        f"/api/v1/move-jobs/{second['job']['id']}/access-links/{customer_link['id']}/revoke",
        headers=_bearer(manager_secret),
    )
    openapi = (await client.get("/openapi.json")).json()
    assert removed_provisioning.status_code == 404
    assert cross_role_rotation.status_code == 403
    assert cross_job_link.status_code == 404
    assert cross_job_path.status_code == 404
    assert "/api/v1/move-jobs/{job_id}/participants" not in openapi["paths"]
    participants_schema = openapi["components"]["schemas"]["MoveJobCreate"]["properties"][
        "participants"
    ]
    assert participants_schema["minItems"] == participants_schema["maxItems"] == 3


@pytest.mark.anyio
async def test_access_link_rotation_expiry_and_revocation(
    access_api: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, factory = access_api
    created = await _create_job(client)
    job_id = created["job"]["id"]
    customer_link, manager_link, _ = created["access_links"]
    customer_id = customer_link["participant_id"]
    old_secret = customer_link["secret"]
    manager_secret = manager_link["secret"]

    forbidden_manager_rotation = await client.post(
        f"/api/v1/move-jobs/{job_id}/participants/{customer_id}/access-links",
        headers=_bearer(manager_secret),
    )
    assert forbidden_manager_rotation.status_code == 403
    rotated = await client.post(
        f"/api/v1/move-jobs/{job_id}/participants/{customer_id}/access-links",
        headers=_bearer(old_secret),
    )
    assert rotated.status_code == 201
    assert rotated.headers["cache-control"] == "no-store"
    new_link = rotated.json()
    new_secret = new_link["secret"]
    assert new_link["id"] == customer_link["id"]
    assert new_link["expires_at"] == customer_link["expires_at"]
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = access_api
    first = await _create_job(client)
    second = await _create_job(client)
    job_id = first["job"]["id"]
    manager_secret = first["access_links"][1]["secret"]

    missing_participant = await client.post(
        f"/api/v1/move-jobs/{job_id}/participants/{uuid4()}/access-links",
        headers=_bearer(manager_secret),
    )
    missing_link = await client.post(
        f"/api/v1/move-jobs/{job_id}/access-links/{uuid4()}/revoke",
        headers=_bearer(manager_secret),
    )
    other_job_link = await client.post(
        f"/api/v1/move-jobs/{job_id}/access-links/{second['access_links'][0]['id']}/revoke",
        headers=_bearer(manager_secret),
    )
    assert missing_participant.status_code == 403
    assert missing_link.status_code == 404
    assert other_job_link.status_code == 404

    async def disappear(*_args: object) -> None:
        return None

    async def invalidate(*_args: object, **_kwargs: object) -> None:
        raise InvalidAccessTokenError

    manager_id = first["access_links"][1]["participant_id"]
    rotation_url = f"/api/v1/move-jobs/{job_id}/participants/{manager_id}/access-links"
    with monkeypatch.context() as context:
        context.setattr("app.modules.access.router.load_participant", disappear)
        assert (await client.post(rotation_url, headers=_bearer(manager_secret))).status_code == 404
    with monkeypatch.context() as context:
        context.setattr("app.modules.access.router.rotate_access_link", invalidate)
        invalidated = await client.post(rotation_url, headers=_bearer(manager_secret))
        assert invalidated.status_code == 401
        assert invalidated.headers["www-authenticate"] == "Bearer"


@pytest.mark.anyio
async def test_database_fallback_limits_valid_access_link_and_resets_window(
    tmp_path: Path,
) -> None:
    settings = Settings(
        environment=AppEnvironment.TEST,
        access_rate_limit_requests=3,
        access_rate_limit_window_seconds=30,
    )
    client, factory, resources = await _access_api_with_settings(tmp_path, settings)
    _, engine = resources
    try:
        created = await _create_job(client)
        job_id = created["job"]["id"]
        customer_link = created["access_links"][0]
        secret = customer_link["secret"]
        participant_id = customer_link["participant_id"]
        url = f"/api/v1/move-jobs/{job_id}"

        assert (await client.get(url, headers=_bearer(secret))).status_code == 200
        rotated = await client.post(
            f"/api/v1/move-jobs/{job_id}/participants/{participant_id}/access-links",
            headers=_bearer(secret),
        )
        assert rotated.status_code == 201
        new_secret = rotated.json()["secret"]
        assert rotated.json()["id"] == customer_link["id"]
        assert (await client.get(url, headers=_bearer(new_secret))).status_code == 200
        rejected = await client.get(url, headers=_bearer(new_secret))

        assert rejected.status_code == 429
        assert rejected.headers["retry-after"] == "30"
        assert rejected.json()["detail"] == "access rate limit exceeded"
        async with factory() as session:
            stored = await session.get(ParticipantAccessToken, UUID(customer_link["id"]))
            assert stored is not None
            assert stored.rate_window_count == 3
            assert stored.last_used_at is not None
            assert secret not in stored.token_hash

        async with factory.begin() as session:
            await session.execute(
                update(ParticipantAccessToken)
                .where(ParticipantAccessToken.id == UUID(customer_link["id"]))
                .values(rate_window_started_at=datetime.now(UTC) - timedelta(seconds=31))
            )
        assert (await client.get(url, headers=_bearer(new_secret))).status_code == 200
        async with factory() as session:
            stored = await session.get(ParticipantAccessToken, UUID(customer_link["id"]))
            assert stored is not None
            assert stored.rate_window_count == 1
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.anyio
async def test_rotation_preserves_redis_and_database_rate_limits(
    tmp_path: Path,
) -> None:
    settings = Settings(
        environment=AppEnvironment.TEST,
        access_rate_limit_requests=2,
        access_rate_limit_window_seconds=10,
    )
    client, factory, resources = await _access_api_with_settings(tmp_path, settings)
    application, engine = resources
    application.state.cache_port = FakeCache()
    try:
        created = await _create_job(client)
        customer_link = created["access_links"][0]
        url = f"/api/v1/move-jobs/{created['job']['id']}"

        assert (await client.get(url, headers=_bearer(customer_link["secret"]))).status_code == 200
        rotated = await client.post(
            f"/api/v1/move-jobs/{created['job']['id']}/participants/"
            f"{customer_link['participant_id']}/access-links",
            headers=_bearer(customer_link["secret"]),
        )
        assert rotated.status_code == 201
        assert rotated.json()["id"] == customer_link["id"]
        assert rotated.json()["expires_at"] == customer_link["expires_at"]

        async with factory() as session:
            stored = await session.get(ParticipantAccessToken, UUID(customer_link["id"]))
            assert stored is not None
            assert stored.rate_window_count == 2
            assert stored.rate_window_started_at is not None
        async with factory.begin() as session:
            await session.execute(
                update(ParticipantAccessToken)
                .where(ParticipantAccessToken.id == UUID(customer_link["id"]))
                .values(rate_window_started_at=None, rate_window_count=0)
            )
        assert (await client.get(url, headers=_bearer(rotated.json()["secret"]))).status_code == 429
    finally:
        await client.aclose()
        await engine.dispose()


class UnavailableCache:
    async def increment_fixed_window(
        self,
        *,
        key: str,
        window_seconds: int,
        timeout_seconds: float,
    ) -> int:
        raise ProviderError(ProviderErrorKind.UNAVAILABLE, "offline", retryable=True)


class OverLimitCache:
    async def increment_fixed_window(
        self,
        *,
        key: str,
        window_seconds: int,
        timeout_seconds: float,
    ) -> int:
        return 2


class MisconfiguredCache:
    async def increment_fixed_window(
        self,
        *,
        key: str,
        window_seconds: int,
        timeout_seconds: float,
    ) -> int:
        raise ProviderError(ProviderErrorKind.PERMISSION_DENIED, "denied", retryable=False)


@pytest.mark.anyio
async def test_redis_outage_uses_database_fallback_but_configuration_error_propagates(
    tmp_path: Path,
) -> None:
    settings = Settings(environment=AppEnvironment.TEST, access_rate_limit_requests=1)
    client, factory, resources = await _access_api_with_settings(tmp_path, settings)
    application, engine = resources
    try:
        created = await _create_job(client)
        customer_link = created["access_links"][0]
        url = f"/api/v1/move-jobs/{created['job']['id']}"
        application.state.cache_port = FakeCache()
        assert (await client.get(url, headers=_bearer(customer_link["secret"]))).status_code == 200
        application.state.cache_port = UnavailableCache()
        assert (await client.get(url, headers=_bearer(customer_link["secret"]))).status_code == 429
        async with factory() as session:
            stored = await session.get(ParticipantAccessToken, UUID(customer_link["id"]))
            assert stored is not None
        assert stored.rate_window_count == 1

        worker_link = created["access_links"][2]
        assert (await client.get(url, headers=_bearer(worker_link["secret"]))).status_code == 200
        async with factory() as session:
            worker = await session.get(ParticipantAccessToken, UUID(worker_link["id"]))
            assert worker is not None
            assert worker.rate_window_count == 1

        application.state.cache_port = OverLimitCache()
        manager_link = created["access_links"][1]
        assert (await client.get(url, headers=_bearer(manager_link["secret"]))).status_code == 429

        application.state.cache_port = MisconfiguredCache()
        with pytest.raises(ProviderError) as error_info:
            await client.get(url, headers=_bearer(manager_link["secret"]))
        assert error_info.value.kind is ProviderErrorKind.PERMISSION_DENIED
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.anyio
async def test_rejected_business_request_still_commits_access_use_history(
    access_api: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, factory = access_api
    created = await _create_job(client)
    customer_link = created["access_links"][0]

    response = await client.get(
        f"/api/v1/move-jobs/{uuid4()}",
        headers=_bearer(customer_link["secret"]),
    )

    assert response.status_code == 404
    async with factory() as session:
        stored = await session.get(ParticipantAccessToken, UUID(customer_link["id"]))
        assert stored is not None
        assert stored.last_used_at is not None
        assert stored.rate_window_count == 1


@pytest.mark.anyio
async def test_database_fallback_rejects_link_revoked_after_authentication(
    access_api: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, factory = access_api
    created = await _create_job(client)
    access_link_id = UUID(created["access_links"][0]["id"])
    participant_id = UUID(created["access_links"][0]["participant_id"])
    async with factory.begin() as session:
        await session.execute(
            update(ParticipantAccessToken)
            .where(ParticipantAccessToken.id == access_link_id)
            .values(revoked_at=datetime.now(UTC))
        )

    async with factory.begin() as session:
        with pytest.raises(InvalidAccessTokenError):
            await _increment_database_rate_window(
                session,
                access_link_id,
                expected_token_hash=hashlib.sha256(
                    created["access_links"][0]["secret"].encode()
                ).hexdigest(),
                now=datetime.now(UTC),
                window_seconds=60,
            )
        participant = await session.get(JobParticipant, participant_id)
        assert participant is not None
        with pytest.raises(InvalidAccessTokenError):
            await rotate_access_link(
                session,
                participant,
                current_secret=created["access_links"][0]["secret"],
                actor_participant_id=participant_id,
            )
