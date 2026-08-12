"""Capture session, media policy, authorization, and upload verification tests."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import AppEnvironment, Settings
from app.contracts.fakes import FakeObjectStorage
from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.ports import ProviderError, ProviderErrorKind, StorageObjectMetadata
from app.main import create_app
from app.modules.capture.models import MediaAsset
from app.modules.capture.schemas import (
    MAX_IMAGE_BYTES,
    MAX_VIDEO_BYTES,
    MediaUploadCreate,
    MediaUploadResponse,
)
from app.modules.capture.service import (
    CaptureResourceNotFoundError,
    MediaPurposeNotAllowedError,
    create_capture_session,
    create_media_upload,
)
from app.platform.db import Base, create_session_factory

CaptureApi = tuple[
    AsyncClient,
    async_sessionmaker[AsyncSession],
    FakeObjectStorage,
    FastAPI,
]


@pytest.fixture
async def capture_api(tmp_path: Path) -> AsyncIterator[CaptureApi]:
    database_path = (tmp_path / "capture.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
    factory = create_session_factory(engine)
    storage = FakeObjectStorage()
    application = create_app(Settings(environment=AppEnvironment.TEST))
    application.state.database_session_factory = factory
    application.state.storage_port = storage
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client, factory, storage, application
    await engine.dispose()


async def _create_job(client: AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/move-jobs",
        json={
            "title": "촬영 테스트",
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
    return cast(dict[str, Any], response.json())


def _secret(created: dict[str, Any], role: str) -> str:
    return cast(
        str,
        next(link["secret"] for link in created["access_links"] if link["role"] == role),
    )


def _headers(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


async def _create_capture(
    client: AsyncClient, created: dict[str, Any], secret: str
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/move-jobs/{created['job']['id']}/capture-sessions",
        headers=_headers(secret),
    )
    assert response.status_code == 201
    return cast(dict[str, Any], response.json())


@pytest.mark.anyio
async def test_capture_upload_verifies_metadata_and_is_idempotent(capture_api: CaptureApi) -> None:
    client, factory, storage, _ = capture_api
    created = await _create_job(client)
    customer_secret = _secret(created, "customer")
    capture = await _create_capture(client, created, customer_secret)
    job = created["job"]
    room_zone_id = job["locations"][0]["room_zones"][0]["id"]
    upload = await client.post(
        f"/api/v1/move-jobs/{job['id']}/capture-sessions/{capture['id']}/media-assets/upload",
        headers=_headers(customer_secret),
        json={
            "room_zone_id": room_zone_id,
            "media_purpose": "inventory",
            "content_type": "image/jpeg",
            "content_length": 12,
        },
    )

    assert upload.status_code == 201
    upload_body = upload.json()
    assert upload_body["asset"]["status"] == "pending_upload"
    assert upload_body["upload_url"].startswith("https://storage.invalid/upload/jobs/")
    parsed_upload = MediaUploadResponse.model_validate(upload_body, strict=False)
    assert "storage.invalid" not in repr(parsed_upload)
    customer_link = next(link for link in created["access_links"] if link["role"] == "customer")
    assert capture["created_by_participant_id"] == customer_link["participant_id"]

    asset_id = UUID(upload_body["asset"]["id"])
    async with factory() as session:
        asset = await session.get(MediaAsset, asset_id)
        assert asset is not None
        object_key = asset.object_key
        assert "url" not in asset.__table__.columns
    storage.metadata[object_key] = StorageObjectMetadata(
        object_key=object_key,
        content_type="image/jpeg",
        size_bytes=12,
        sha256_hex="a" * 64,
        generation="1",
    )

    complete_url = (
        f"/api/v1/move-jobs/{job['id']}/capture-sessions/{capture['id']}"
        f"/media-assets/{asset_id}/complete"
    )
    completed = await client.post(complete_url, headers=_headers(customer_secret))
    assert completed.status_code == 200
    assert completed.json()["status"] == "uploaded"
    assert completed.json()["actual_size_bytes"] == 12
    assert completed.json()["sha256_hex"] == "a" * 64

    storage.metadata.clear()
    repeated = await client.post(complete_url, headers=_headers(customer_secret))
    assert repeated.status_code == 200
    assert repeated.json()["id"] == completed.json()["id"]
    assert repeated.json()["status"] == "uploaded"
    assert repeated.json()["actual_size_bytes"] == 12


@pytest.mark.anyio
async def test_capture_enforces_actor_purpose_zone_and_input_boundaries(
    capture_api: CaptureApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory, storage, application = capture_api
    first = await _create_job(client)
    second = await _create_job(client)
    job = first["job"]
    customer_secret = _secret(first, "customer")
    worker_secret = _secret(first, "field_worker")

    unauthenticated = await client.post(f"/api/v1/move-jobs/{job['id']}/capture-sessions")
    assert unauthenticated.status_code == 401
    cross_job = await client.post(
        f"/api/v1/move-jobs/{second['job']['id']}/capture-sessions",
        headers=_headers(customer_secret),
    )
    assert cross_job.status_code == 404

    capture = await _create_capture(client, first, customer_secret)
    upload_url = (
        f"/api/v1/move-jobs/{job['id']}/capture-sessions/{capture['id']}/media-assets/upload"
    )
    valid_payload = {
        "room_zone_id": job["locations"][0]["room_zones"][0]["id"],
        "media_purpose": "condition",
        "content_type": "image/png",
        "content_length": 1,
    }

    not_owner = await client.post(
        upload_url,
        headers=_headers(worker_secret),
        json=valid_payload,
    )
    assert not_owner.status_code == 404
    completion_upload = await client.post(
        upload_url,
        headers=_headers(customer_secret),
        json={**valid_payload, "media_purpose": "completion"},
    )
    assert completion_upload.status_code == 201
    foreign_zone = await client.post(
        upload_url,
        headers=_headers(customer_secret),
        json={
            **valid_payload,
            "room_zone_id": second["job"]["locations"][0]["room_zones"][0]["id"],
        },
    )
    assert foreign_zone.status_code == 404

    invalid_payloads = (
        {**valid_payload, "content_length": MAX_IMAGE_BYTES + 1},
        {
            **valid_payload,
            "content_type": "video/mp4",
            "content_length": MAX_VIDEO_BYTES + 1,
        },
        {**valid_payload, "content_type": "application/pdf"},
        {**valid_payload, "content_length": 0},
    )
    for payload in invalid_payloads:
        assert (
            await client.post(upload_url, headers=_headers(customer_secret), json=payload)
        ).status_code == 422

    del application.state.storage_port
    unavailable = await client.post(
        upload_url,
        headers=_headers(customer_secret),
        json=valid_payload,
    )
    hidden_cross_job = await client.post(
        upload_url.replace(str(job["id"]), str(second["job"]["id"]), 1),
        headers=_headers(customer_secret),
        json=valid_payload,
    )
    assert unavailable.status_code == 503
    assert hidden_cross_job.status_code == 404
    application.state.storage_port = storage

    async with factory() as session:
        with pytest.raises(CaptureResourceNotFoundError):
            await create_capture_session(session, UUID(job["id"]), uuid4())

    async def raise_missing(*_args: object) -> None:
        raise CaptureResourceNotFoundError(job["id"])

    monkeypatch.setattr("app.modules.capture.router.create_capture_session", raise_missing)
    service_missing = await client.post(
        f"/api/v1/move-jobs/{job['id']}/capture-sessions",
        headers=_headers(customer_secret),
    )
    assert service_missing.status_code == 404

    async with factory() as session:
        monkeypatch.setattr("app.modules.capture.service.CAPTURE_PURPOSES", frozenset())
        with pytest.raises(MediaPurposeNotAllowedError):
            await create_media_upload(
                session,
                storage,
                UUID(job["id"]),
                UUID(capture["id"]),
                UUID(
                    next(
                        participant["id"]
                        for participant in job["participants"]
                        if participant["role"] == "customer"
                    )
                ),
                MediaUploadCreate(
                    room_zone_id=UUID(job["locations"][0]["room_zones"][0]["id"]),
                    media_purpose=MediaPurpose.INVENTORY,
                    content_type="image/jpeg",
                    content_length=1,
                ),
            )

    async def reject_purpose(*_args: object) -> None:
        raise MediaPurposeNotAllowedError(MediaPurpose.INVENTORY)

    monkeypatch.setattr("app.modules.capture.router.create_media_upload", reject_purpose)
    purpose_error = await client.post(
        upload_url,
        headers=_headers(customer_secret),
        json=valid_payload,
    )
    assert purpose_error.status_code == 422


@pytest.mark.anyio
async def test_capture_maps_provider_failure_and_upload_conflicts(
    capture_api: CaptureApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory, storage, _ = capture_api
    created = await _create_job(client)
    secret = _secret(created, "customer")
    capture = await _create_capture(client, created, secret)
    job = created["job"]
    upload_url = (
        f"/api/v1/move-jobs/{job['id']}/capture-sessions/{capture['id']}/media-assets/upload"
    )
    payload = {
        "room_zone_id": job["locations"][0]["room_zones"][0]["id"],
        "media_purpose": "inventory",
        "content_type": "video/mp4",
        "content_length": 50,
    }

    original_create_upload_url = storage.create_upload_url

    async def fail_upload_url(**_kwargs: object) -> str:
        raise ProviderError(ProviderErrorKind.UNAVAILABLE, "unavailable", retryable=True)

    monkeypatch.setattr(storage, "create_upload_url", fail_upload_url)
    provider_failed = await client.post(upload_url, headers=_headers(secret), json=payload)
    assert provider_failed.status_code == 503
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(MediaAsset)) == 0
    monkeypatch.setattr(storage, "create_upload_url", original_create_upload_url)

    uploaded = await client.post(upload_url, headers=_headers(secret), json=payload)
    assert uploaded.status_code == 201
    asset_id = UUID(uploaded.json()["asset"]["id"])
    complete_url = (
        f"/api/v1/move-jobs/{job['id']}/capture-sessions/{capture['id']}"
        f"/media-assets/{asset_id}/complete"
    )
    missing_object = await client.post(complete_url, headers=_headers(secret))
    assert missing_object.status_code == 409

    async with factory() as session:
        asset = await session.get(MediaAsset, asset_id)
        assert asset is not None
        object_key = asset.object_key

    mismatches = (
        StorageObjectMetadata(
            object_key=f"{object_key}-other",
            content_type="video/mp4",
            size_bytes=50,
        ),
        StorageObjectMetadata(
            object_key=object_key,
            content_type="image/jpeg",
            size_bytes=50,
        ),
        StorageObjectMetadata(
            object_key=object_key,
            content_type="video/mp4",
            size_bytes=51,
        ),
    )
    for metadata in mismatches:
        storage.metadata[object_key] = metadata
        mismatch = await client.post(complete_url, headers=_headers(secret))
        assert mismatch.status_code == 409

    original_get_metadata = storage.get_metadata

    async def fail_metadata(**_kwargs: object) -> StorageObjectMetadata:
        raise ProviderError(ProviderErrorKind.DEADLINE_EXCEEDED, "timeout", retryable=True)

    monkeypatch.setattr(storage, "get_metadata", fail_metadata)
    provider_timeout = await client.post(complete_url, headers=_headers(secret))
    assert provider_timeout.status_code == 503
    monkeypatch.setattr(storage, "get_metadata", original_get_metadata)

    missing_asset = await client.post(
        complete_url.replace(str(asset_id), str(uuid4())),
        headers=_headers(secret),
    )
    assert missing_asset.status_code == 404

    storage.metadata[object_key] = StorageObjectMetadata(
        object_key=object_key,
        content_type="video/mp4",
        size_bytes=50,
    )
    completed = await client.post(complete_url, headers=_headers(secret))
    assert completed.status_code == 200
    storage.metadata.clear()
    repeated = await client.post(complete_url, headers=_headers(secret))
    assert repeated.status_code == 200

    async with factory.begin() as session:
        await session.execute(
            update(MediaAsset)
            .where(MediaAsset.id == asset_id)
            .values(status=MediaAssetStatus.READY)
        )
    state_conflict = await client.post(complete_url, headers=_headers(secret))
    assert state_conflict.status_code == 409
