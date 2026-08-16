"""Capture session, media policy, authorization, and upload verification tests."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import AppEnvironment, Settings
from app.contracts.fakes import FakeObjectStorage
from app.contracts.maintenance import BackgroundJobType
from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.ports import (
    ProviderError,
    ProviderErrorKind,
    StorageObjectMetadata,
    StorageUploadTarget,
)
from app.main import create_app
from app.modules.analysis_workflow.models import CaptureAnalysisDispatch
from app.modules.background_job.models import BackgroundJob, BackgroundJobStatus
from app.modules.capture.models import CaptureSession, MediaAsset
from app.modules.capture.schemas import (
    MAX_IMAGE_BYTES,
    MAX_VIDEO_BYTES,
    MEDIA_CONSENT_POLICY_VERSION,
    CaptureSessionCreate,
    MediaUploadCreate,
    MediaUploadResponse,
)
from app.modules.capture.service import (
    CaptureResourceNotFoundError,
    MediaConsentConflictError,
    MediaPurposeNotAllowedError,
    MediaUploadStateConflictError,
    complete_media_upload,
    create_capture_session,
    create_media_upload,
)
from app.modules.move_job.models import MoveJob, MoveJobStatus
from app.platform.db import Base, create_session_factory

CaptureApi = tuple[
    AsyncClient,
    async_sessionmaker[AsyncSession],
    FakeObjectStorage,
    FastAPI,
]
MEDIA_CONSENT_PAYLOAD = {
    "consent_policy_version": MEDIA_CONSENT_POLICY_VERSION,
    "privacy_notice_acknowledged": True,
}


def _upload_target(
    url: str,
    content_type: str,
    *extra_headers: tuple[str, str],
) -> StorageUploadTarget:
    return StorageUploadTarget(
        url=url,
        headers=(
            ("Content-Type", content_type),
            ("x-goog-if-generation-match", "0"),
            *extra_headers,
        ),
    )


@pytest.fixture
async def capture_api(tmp_path: Path) -> AsyncIterator[CaptureApi]:
    database_path = (tmp_path / "capture.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
    factory = create_session_factory(engine)
    storage = FakeObjectStorage()
    application = create_app(Settings(environment=AppEnvironment.TEST, media_retention_days=30))
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
        json=MEDIA_CONSENT_PAYLOAD,
    )
    assert response.status_code == 201
    assert response.json()["media_processing_consent"] == {
        "policy_version": MEDIA_CONSENT_POLICY_VERSION,
        "processing_purposes": [
            "inventory_analysis",
            "condition_record",
            "field_change_evidence",
            "completion_record",
        ],
        "privacy_notice_acknowledged": True,
        "retention_days_after_job_completion": 30,
        "consented_at": response.json()["media_processing_consent"]["consented_at"],
    }
    return cast(dict[str, Any], response.json())


@pytest.mark.anyio
async def test_media_consent_policy_and_explicit_acknowledgement(
    capture_api: CaptureApi,
) -> None:
    client, factory, storage, _ = capture_api
    created = await _create_job(client)
    secret = _secret(created, "customer")
    job_id = created["job"]["id"]
    policy_url = f"/api/v1/move-jobs/{job_id}/media-consent-policy"
    capture_url = f"/api/v1/move-jobs/{job_id}/capture-sessions"

    policy = await client.get(policy_url, headers=_headers(secret))
    assert policy.status_code == 200
    assert policy.json()["policy_version"] == MEDIA_CONSENT_POLICY_VERSION
    assert policy.json()["retention_days_after_job_completion"] == 30
    assert "AI 결과는 초안" in policy.json()["notice"]
    assert (await client.get(policy_url)).status_code == 401

    assert (await client.post(capture_url, headers=_headers(secret), json={})).status_code == 422
    assert (
        await client.post(
            capture_url,
            headers=_headers(secret),
            json={
                **MEDIA_CONSENT_PAYLOAD,
                "privacy_notice_acknowledged": False,
            },
        )
    ).status_code == 422
    stale = await client.post(
        capture_url,
        headers=_headers(secret),
        json={**MEDIA_CONSENT_PAYLOAD, "consent_policy_version": "stale.v1"},
    )
    assert stale.status_code == 409

    no_retention_app = create_app(
        Settings(environment=AppEnvironment.TEST, media_retention_days=None)
    )
    no_retention_app.state.database_session_factory = factory
    no_retention_app.state.storage_port = storage
    async with AsyncClient(
        transport=ASGITransport(app=no_retention_app),
        base_url="http://testserver",
    ) as no_retention_client:
        assert (
            await no_retention_client.get(policy_url, headers=_headers(secret))
        ).status_code == 503
        assert (
            await no_retention_client.post(
                capture_url,
                headers=_headers(secret),
                json=MEDIA_CONSENT_PAYLOAD,
            )
        ).status_code == 503


@pytest.mark.anyio
async def test_capture_session_list_recovers_owned_media_and_analysis_state(
    capture_api: CaptureApi,
) -> None:
    client, factory, _, _ = capture_api
    created = await _create_job(client)
    job = created["job"]
    customer_secret = _secret(created, "customer")
    manager_secret = _secret(created, "company_manager")
    worker_secret = _secret(created, "field_worker")
    submitted_capture = await _create_capture(client, created, customer_secret)
    draft_capture = await _create_capture(client, created, customer_secret)
    worker_capture = await _create_capture(client, created, worker_secret)
    room_zone_id = UUID(job["locations"][0]["room_zones"][0]["id"])

    ready_asset = MediaAsset(
        capture_session_id=UUID(submitted_capture["id"]),
        room_zone_id=room_zone_id,
        media_purpose=MediaPurpose.INVENTORY,
        status=MediaAssetStatus.READY,
        object_key=(f"jobs/{job['id']}/captures/{submitted_capture['id']}/{uuid4()}"),
        content_type="image/jpeg",
        expected_size_bytes=12,
        actual_size_bytes=12,
        sha256_hex="a" * 64,
        generation="7",
        uploaded_at=datetime.now(UTC),
    )
    pending_asset = MediaAsset(
        capture_session_id=UUID(draft_capture["id"]),
        room_zone_id=room_zone_id,
        media_purpose=MediaPurpose.INVENTORY,
        status=MediaAssetStatus.PENDING_UPLOAD,
        object_key=f"jobs/{job['id']}/captures/{draft_capture['id']}/{uuid4()}",
        content_type="video/mp4",
        expected_size_bytes=24,
    )
    async with factory.begin() as session:
        session.add_all((ready_asset, pending_asset))

    submitted = await client.post(
        f"/api/v1/move-jobs/{job['id']}/capture-sessions/{submitted_capture['id']}/submit",
        headers=_headers(customer_secret),
    )
    assert submitted.status_code == 202

    list_url = f"/api/v1/move-jobs/{job['id']}/capture-sessions"
    customer_response = await client.get(list_url, headers=_headers(customer_secret))
    manager_response = await client.get(list_url, headers=_headers(manager_secret))
    worker_response = await client.get(list_url, headers=_headers(worker_secret))

    assert customer_response.status_code == 200
    assert manager_response.json() == []
    customer_sessions = {
        item["id"]: item for item in cast(list[dict[str, Any]], customer_response.json())
    }
    assert set(customer_sessions) == {submitted_capture["id"], draft_capture["id"]}
    worker_sessions = cast(list[dict[str, Any]], worker_response.json())
    assert len(worker_sessions) == 1
    assert worker_sessions[0]["id"] == worker_capture["id"]
    assert worker_sessions[0]["media_processing_consent"]["policy_version"] == (
        MEDIA_CONSENT_POLICY_VERSION
    )
    assert worker_sessions[0]["media_assets"] == []
    assert worker_sessions[0]["analysis"] is None

    submitted_view = customer_sessions[submitted_capture["id"]]
    assert submitted_view["analysis"]["analysis_run_id"] == submitted.json()["analysis_run_id"]
    assert submitted_view["analysis"]["status"] == "pending"
    assert submitted_view["media_assets"][0]["status"] == "ready"
    assert set(submitted_view["media_assets"][0]) == {
        "id",
        "capture_session_id",
        "room_zone_id",
        "media_purpose",
        "status",
        "content_type",
        "expected_size_bytes",
        "actual_size_bytes",
        "sha256_hex",
        "created_at",
        "uploaded_at",
    }
    draft_view = customer_sessions[draft_capture["id"]]
    assert draft_view["analysis"] is None
    assert draft_view["media_assets"][0]["status"] == "pending_upload"
    assert "object_key" not in customer_response.text
    assert "generation" not in customer_response.text
    assert "provider_task_id" not in customer_response.text
    assert customer_secret not in customer_response.text


@pytest.mark.anyio
async def test_capture_upload_verifies_metadata_and_is_idempotent(
    capture_api: CaptureApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory, storage, _ = capture_api
    created = await _create_job(client)
    customer_secret = _secret(created, "customer")
    capture = await _create_capture(client, created, customer_secret)
    job = created["job"]
    room_zone_id = job["locations"][0]["room_zones"][0]["id"]
    upload_path = (
        f"/api/v1/move-jobs/{job['id']}/capture-sessions/{capture['id']}/media-assets/upload"
    )
    upload_payload = {
        "room_zone_id": room_zone_id,
        "media_purpose": "inventory",
        "content_type": "image/jpeg",
        "content_length": 12,
    }
    signed_upload_url = "https://storage.invalid/upload/%2F?X-Goog-Signature=A%2B&object=jobs%2F1"

    async def encoded_upload_url(**_kwargs: object) -> StorageUploadTarget:
        return _upload_target(
            signed_upload_url,
            "image/jpeg",
            ("X-Signed-Exact", "  keep both spaces  "),
        )

    monkeypatch.setattr(storage, "create_upload_url", encoded_upload_url)
    upload = await client.post(
        upload_path,
        headers=_headers(customer_secret),
        json=upload_payload,
    )
    pending_upload = await client.post(
        upload_path,
        headers=_headers(customer_secret),
        json=upload_payload,
    )

    assert upload.status_code == 201
    assert pending_upload.status_code == 201
    assert upload.headers["cache-control"] == "no-store"
    upload_body = upload.json()
    assert upload_body["asset"]["status"] == "pending_upload"
    assert upload_body["upload_url"] == signed_upload_url
    assert upload_body["upload_headers"] == {
        "Content-Type": "image/jpeg",
        "x-goog-if-generation-match": "0",
        "X-Signed-Exact": "  keep both spaces  ",
    }
    parsed_upload = MediaUploadResponse.model_validate(upload_body, strict=False)
    assert parsed_upload.upload_headers == upload_body["upload_headers"]
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
    assert completed.json()["sha256_hex"] is None

    async with factory() as session:
        validation = await session.scalar(
            select(BackgroundJob).where(
                BackgroundJob.media_asset_id == asset_id,
                BackgroundJob.job_type == BackgroundJobType.MEDIA_VALIDATION,
            )
        )
        assert validation is not None
        assert validation.status is BackgroundJobStatus.PENDING
        assert validation.target_generation == "1"
        assert validation.target_content_type == "image/jpeg"
        assert validation.target_size_bytes == 12

    async with factory.begin() as session:
        move_job = await session.get(MoveJob, UUID(job["id"]))
        assert move_job is not None
        move_job.status = MoveJobStatus.CANCELED

    storage.metadata.clear()
    repeated = await client.post(complete_url, headers=_headers(customer_secret))
    assert repeated.status_code == 200
    assert repeated.json()["id"] == completed.json()["id"]
    assert repeated.json()["status"] == "uploaded"
    assert repeated.json()["actual_size_bytes"] == 12
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(BackgroundJob)
                .where(
                    BackgroundJob.media_asset_id == asset_id,
                    BackgroundJob.job_type == BackgroundJobType.MEDIA_VALIDATION,
                )
            )
            == 1
        )

    async def unexpected_storage(**_kwargs: object) -> object:
        raise AssertionError("terminal capture must not call storage")

    monkeypatch.setattr(storage, "get_metadata", unexpected_storage)
    monkeypatch.setattr(storage, "create_upload_url", unexpected_storage)
    new_capture = await client.post(
        f"/api/v1/move-jobs/{job['id']}/capture-sessions",
        headers=_headers(customer_secret),
        json=MEDIA_CONSENT_PAYLOAD,
    )
    new_upload = await client.post(
        upload_path,
        headers=_headers(customer_secret),
        json=upload_payload,
    )
    pending_asset_id = pending_upload.json()["asset"]["id"]
    pending_complete = await client.post(
        f"/api/v1/move-jobs/{job['id']}/capture-sessions/{capture['id']}"
        f"/media-assets/{pending_asset_id}/complete",
        headers=_headers(customer_secret),
    )
    assert new_capture.status_code == 409
    assert new_upload.status_code == 409
    assert pending_complete.status_code == 409

    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CaptureSession)) == 1
        assert await session.scalar(select(func.count()).select_from(MediaAsset)) == 2
        pending_asset = await session.get(MediaAsset, UUID(pending_asset_id))
        assert pending_asset is not None
        assert pending_asset.status is MediaAssetStatus.PENDING_UPLOAD


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

    unauthenticated = await client.post(
        f"/api/v1/move-jobs/{job['id']}/capture-sessions",
        json=MEDIA_CONSENT_PAYLOAD,
    )
    assert unauthenticated.status_code == 401
    cross_job = await client.post(
        f"/api/v1/move-jobs/{second['job']['id']}/capture-sessions",
        headers=_headers(customer_secret),
        json=MEDIA_CONSENT_PAYLOAD,
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
            await create_capture_session(
                session,
                uuid4(),
                uuid4(),
                CaptureSessionCreate.model_validate(MEDIA_CONSENT_PAYLOAD),
                retention_days=30,
            )
        with pytest.raises(CaptureResourceNotFoundError):
            await create_capture_session(
                session,
                UUID(job["id"]),
                uuid4(),
                CaptureSessionCreate.model_validate(MEDIA_CONSENT_PAYLOAD),
                retention_days=30,
            )

    async def raise_missing(*_args: object, **_kwargs: object) -> None:
        raise CaptureResourceNotFoundError(job["id"])

    monkeypatch.setattr("app.modules.capture.router.create_capture_session", raise_missing)
    service_missing = await client.post(
        f"/api/v1/move-jobs/{job['id']}/capture-sessions",
        headers=_headers(customer_secret),
        json=MEDIA_CONSENT_PAYLOAD,
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

    customer_participant_id = UUID(
        next(
            participant["id"]
            for participant in job["participants"]
            if participant["role"] == "customer"
        )
    )
    async with factory.begin() as session:
        legacy_capture = CaptureSession(
            job_id=UUID(job["id"]),
            created_by_participant_id=customer_participant_id,
        )
        session.add(legacy_capture)
        await session.flush()
        legacy_capture_id = legacy_capture.id
    async with factory() as session:
        with pytest.raises(MediaConsentConflictError):
            await create_media_upload(
                session,
                storage,
                UUID(job["id"]),
                legacy_capture_id,
                customer_participant_id,
                MediaUploadCreate.model_validate(valid_payload),
            )
        with pytest.raises(MediaConsentConflictError):
            await complete_media_upload(
                session,
                storage,
                UUID(job["id"]),
                legacy_capture_id,
                uuid4(),
                customer_participant_id,
            )

    async def reject_consent(*_args: object, **_kwargs: object) -> None:
        raise MediaConsentConflictError(legacy_capture_id)

    monkeypatch.setattr("app.modules.capture.router.create_media_upload", reject_consent)
    assert (
        await client.post(
            upload_url,
            headers=_headers(customer_secret),
            json=valid_payload,
        )
    ).status_code == 409
    monkeypatch.setattr("app.modules.capture.router.complete_media_upload", reject_consent)
    assert (
        await client.post(
            f"/api/v1/move-jobs/{job['id']}/capture-sessions/{capture['id']}"
            f"/media-assets/{uuid4()}/complete",
            headers=_headers(customer_secret),
        )
    ).status_code == 409


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

    async def fail_upload_url(**_kwargs: object) -> StorageUploadTarget:
        raise ProviderError(ProviderErrorKind.UNAVAILABLE, "unavailable", retryable=True)

    monkeypatch.setattr(storage, "create_upload_url", fail_upload_url)
    provider_failed = await client.post(upload_url, headers=_headers(secret), json=payload)
    assert provider_failed.status_code == 503
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(MediaAsset)) == 0

    for invalid_url in (
        "http://storage.invalid/upload?signature=secret",
        "https:///upload?signature=secret",
        " https://storage.invalid/upload?signature=secret ",
        "https://storage.invalid:not-a-port/upload?signature=secret",
        "https://storage.invalid:70000/upload?signature=secret",
    ):

        async def invalid_upload_url(
            _url: str = invalid_url,
            **_kwargs: object,
        ) -> StorageUploadTarget:
            return _upload_target(_url, "video/mp4")

        monkeypatch.setattr(storage, "create_upload_url", invalid_upload_url)
        invalid_provider = await client.post(upload_url, headers=_headers(secret), json=payload)
        assert invalid_provider.status_code == 503
        assert "signature=secret" not in invalid_provider.text
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
            generation="1",
        ),
        StorageObjectMetadata(
            object_key=object_key,
            content_type="image/jpeg",
            size_bytes=50,
            generation="1",
        ),
        StorageObjectMetadata(
            object_key=object_key,
            content_type="video/mp4",
            size_bytes=51,
            generation="1",
        ),
    )
    for metadata in mismatches:
        storage.metadata[object_key] = metadata
        mismatch = await client.post(complete_url, headers=_headers(secret))
        assert mismatch.status_code == 409

    storage.metadata[object_key] = StorageObjectMetadata(
        object_key=object_key,
        content_type="video/mp4",
        size_bytes=50,
    )
    missing_generation = await client.post(complete_url, headers=_headers(secret))
    assert missing_generation.status_code == 409
    async with factory() as session:
        asset = await session.get(MediaAsset, asset_id)
        assert asset is not None
        assert asset.status is MediaAssetStatus.PENDING_UPLOAD
        assert asset.actual_size_bytes is None
        assert asset.generation is None

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
        generation="1",
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


@pytest.mark.anyio
async def test_complete_rechecks_state_after_metadata() -> None:
    job_id = uuid4()
    capture_id = uuid4()
    asset_id = uuid4()
    participant_id = uuid4()
    capture = CaptureSession(
        id=capture_id,
        job_id=job_id,
        created_by_participant_id=participant_id,
        media_consent_policy_version=MEDIA_CONSENT_POLICY_VERSION,
        privacy_notice_acknowledged=True,
        media_retention_days=30,
        media_consented_at=datetime.now(UTC),
    )
    pending = MediaAsset(
        id=asset_id,
        capture_session_id=capture_id,
        room_zone_id=uuid4(),
        media_purpose=MediaPurpose.INVENTORY,
        status=MediaAssetStatus.PENDING_UPLOAD,
        object_key="jobs/raced-object",
        content_type="image/jpeg",
        expected_size_bytes=10,
    )
    raced = MediaAsset(
        id=asset_id,
        capture_session_id=capture_id,
        room_zone_id=pending.room_zone_id,
        media_purpose=MediaPurpose.INVENTORY,
        status=MediaAssetStatus.READY,
        object_key=pending.object_key,
        content_type="image/jpeg",
        expected_size_bytes=10,
        actual_size_bytes=10,
        generation="2",
        uploaded_at=datetime.now(UTC),
    )

    def scalar_result(*, one_or_none: object = None, one: object = None) -> MagicMock:
        result = MagicMock()
        result.one_or_none.return_value = one_or_none
        result.one.return_value = one
        return result

    session = AsyncMock(spec=AsyncSession)
    session.scalars.side_effect = (
        scalar_result(one_or_none=capture),
        scalar_result(one_or_none=pending),
        scalar_result(one=raced),
    )
    session.scalar.side_effect = (
        MoveJobStatus.DRAFT,
        MoveJob(id=job_id, title="race", status=MoveJobStatus.DRAFT),
        capture,
        None,
    )
    storage = FakeObjectStorage()
    storage.metadata[pending.object_key] = StorageObjectMetadata(
        object_key=pending.object_key,
        content_type=pending.content_type,
        size_bytes=pending.expected_size_bytes,
        generation="1",
    )

    with pytest.raises(MediaUploadStateConflictError):
        await complete_media_upload(
            session,
            storage,
            job_id,
            capture_id,
            asset_id,
            participant_id,
        )


@pytest.mark.anyio
async def test_capture_submit_status_and_media_freeze_api(
    capture_api: CaptureApi,
) -> None:
    client, factory, storage, _ = capture_api
    created = await _create_job(client)
    job = created["job"]
    customer_secret = _secret(created, "customer")
    worker_secret = _secret(created, "field_worker")
    capture = await _create_capture(client, created, customer_secret)
    submit_url = f"/api/v1/move-jobs/{job['id']}/capture-sessions/{capture['id']}/submit"
    status_url = f"/api/v1/move-jobs/{job['id']}/capture-sessions/{capture['id']}/analysis"

    assert (await client.get(status_url, headers=_headers(customer_secret))).status_code == 404
    assert (await client.post(submit_url, headers=_headers(worker_secret))).status_code == 404
    assert (await client.post(submit_url, headers=_headers(customer_secret))).status_code == 409

    capture_id = UUID(capture["id"])
    room_zone_id = UUID(job["locations"][0]["room_zones"][0]["id"])
    ready_asset = MediaAsset(
        capture_session_id=capture_id,
        room_zone_id=room_zone_id,
        media_purpose=MediaPurpose.INVENTORY,
        status=MediaAssetStatus.READY,
        object_key=f"jobs/{job['id']}/captures/{capture['id']}/{uuid4()}",
        content_type="image/jpeg",
        expected_size_bytes=10,
        actual_size_bytes=10,
        sha256_hex="a" * 64,
        generation="7",
        uploaded_at=datetime.now(UTC),
    )
    pending_condition = MediaAsset(
        capture_session_id=capture_id,
        room_zone_id=room_zone_id,
        media_purpose=MediaPurpose.CONDITION,
        status=MediaAssetStatus.PENDING_UPLOAD,
        object_key=f"jobs/{job['id']}/captures/{capture['id']}/{uuid4()}",
        content_type="image/jpeg",
        expected_size_bytes=10,
    )
    async with factory.begin() as session:
        session.add_all((ready_asset, pending_condition))

    submitted = await client.post(submit_url, headers=_headers(customer_secret))
    repeated = await client.post(submit_url, headers=_headers(customer_secret))
    status_response = await client.get(status_url, headers=_headers(customer_secret))

    assert submitted.status_code == 202
    assert repeated.status_code == 202
    assert status_response.status_code == 200
    assert submitted.json()["analysis_run_id"] == repeated.json()["analysis_run_id"]
    assert status_response.json()["status"] == "pending"
    assert status_response.json()["scope_version_id"] is None
    assert (await client.get(status_url, headers=_headers(worker_secret))).status_code == 404

    upload_url = (
        f"/api/v1/move-jobs/{job['id']}/capture-sessions/{capture['id']}/media-assets/upload"
    )
    frozen_upload = await client.post(
        upload_url,
        headers=_headers(customer_secret),
        json={
            "room_zone_id": str(room_zone_id),
            "media_purpose": "inventory",
            "content_type": "image/jpeg",
            "content_length": 10,
        },
    )
    assert frozen_upload.status_code == 409

    storage.metadata[pending_condition.object_key] = StorageObjectMetadata(
        object_key=pending_condition.object_key,
        content_type="image/jpeg",
        size_bytes=10,
        generation="8",
    )
    frozen_complete = await client.post(
        f"/api/v1/move-jobs/{job['id']}/capture-sessions/{capture['id']}"
        f"/media-assets/{pending_condition.id}/complete",
        headers=_headers(customer_secret),
    )
    assert frozen_complete.status_code == 409

    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CaptureAnalysisDispatch)) == 1
        pending = await session.get(MediaAsset, pending_condition.id)
        assert pending is not None
        assert pending.status is MediaAssetStatus.PENDING_UPLOAD


@pytest.mark.anyio
async def test_upload_creation_rechecks_capture_after_signing() -> None:
    job_id = uuid4()
    capture_id = uuid4()
    participant_id = uuid4()
    room_zone_id = uuid4()
    capture = CaptureSession(
        id=capture_id,
        job_id=job_id,
        created_by_participant_id=participant_id,
        media_consent_policy_version=MEDIA_CONSENT_POLICY_VERSION,
        privacy_notice_acknowledged=True,
        media_retention_days=30,
        media_consented_at=datetime.now(UTC),
    )
    selected = MagicMock()
    selected.one_or_none.return_value = capture
    session = AsyncMock(spec=AsyncSession)
    session.scalars.return_value = selected
    session.scalar.side_effect = (
        MoveJobStatus.DRAFT,
        room_zone_id,
        MoveJob(id=job_id, title="signed race", status=MoveJobStatus.DRAFT),
        None,
    )

    with pytest.raises(CaptureResourceNotFoundError):
        await create_media_upload(
            session,
            FakeObjectStorage(),
            job_id,
            capture_id,
            participant_id,
            MediaUploadCreate(
                room_zone_id=room_zone_id,
                media_purpose=MediaPurpose.INVENTORY,
                content_type="image/jpeg",
                content_length=10,
            ),
        )


@pytest.mark.anyio
async def test_complete_accepts_concurrent_uploaded_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    capture_id = uuid4()
    asset_id = uuid4()
    participant_id = uuid4()
    room_zone_id = uuid4()
    capture = CaptureSession(
        id=capture_id,
        job_id=job_id,
        created_by_participant_id=participant_id,
        media_consent_policy_version=MEDIA_CONSENT_POLICY_VERSION,
        privacy_notice_acknowledged=True,
        media_retention_days=30,
        media_consented_at=datetime.now(UTC),
    )
    pending = MediaAsset(
        id=asset_id,
        capture_session_id=capture_id,
        room_zone_id=room_zone_id,
        media_purpose=MediaPurpose.INVENTORY,
        status=MediaAssetStatus.PENDING_UPLOAD,
        object_key="jobs/concurrent-upload",
        content_type="image/jpeg",
        expected_size_bytes=10,
    )
    raced = MediaAsset(
        id=asset_id,
        capture_session_id=capture_id,
        room_zone_id=room_zone_id,
        media_purpose=MediaPurpose.INVENTORY,
        status=MediaAssetStatus.UPLOADED,
        object_key=pending.object_key,
        content_type="image/jpeg",
        expected_size_bytes=10,
        actual_size_bytes=10,
        generation="2",
        created_at=datetime.now(UTC),
        uploaded_at=datetime.now(UTC),
    )

    def scalar_result(*, one_or_none: object = None, one: object = None) -> MagicMock:
        result = MagicMock()
        result.one_or_none.return_value = one_or_none
        result.one.return_value = one
        return result

    session = AsyncMock(spec=AsyncSession)
    session.scalars.side_effect = (
        scalar_result(one_or_none=capture),
        scalar_result(one_or_none=pending),
        scalar_result(one=raced),
    )
    session.scalar.side_effect = (
        MoveJobStatus.DRAFT,
        MoveJob(id=job_id, title="complete race", status=MoveJobStatus.DRAFT),
        capture,
        None,
    )
    storage = FakeObjectStorage()
    storage.metadata[pending.object_key] = StorageObjectMetadata(
        object_key=pending.object_key,
        content_type="image/jpeg",
        size_bytes=10,
        generation="1",
    )
    create_validation = AsyncMock()
    monkeypatch.setattr(
        "app.modules.capture.service.create_media_validation_background_job",
        create_validation,
    )

    response = await complete_media_upload(
        session,
        storage,
        job_id,
        capture_id,
        asset_id,
        participant_id,
    )

    assert response.status is MediaAssetStatus.UPLOADED
    create_validation.assert_awaited_once()
