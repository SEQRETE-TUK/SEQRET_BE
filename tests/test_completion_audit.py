"""Completion confirmation, state transition, and audit history tests."""

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import create_engine, delete, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import AppEnvironment, Settings
from app.contracts.actor import ParticipantRole
from app.contracts.fakes import FakeObjectStorage
from app.contracts.maintenance import BackgroundJobType
from app.contracts.ports import StorageObjectMetadata
from app.main import create_app
from app.modules.background_job.models import BackgroundJob, BackgroundJobStatus
from app.modules.background_job.service import claim_background_jobs
from app.modules.capture.models import MediaAsset
from app.modules.completion.models import AuditEvent, CompletionConfirmation
from app.modules.completion.schemas import CompletionConfirmationCreate
from app.modules.completion.service import (
    CompletionConflictError,
    CompletionResourceNotFoundError,
    confirm_completion,
)
from app.modules.move_job.models import MoveJob, MoveJobStatus
from app.modules.scope.models import ScopeVersion
from app.modules.scope.schemas import ChangeRequestCreate, ScopeContent
from app.modules.scope.service import (
    ScopeResourceNotFoundError,
    create_change_request,
)
from app.platform.db import Base, create_session_factory
from app.runtime import RuntimeKind, create_runtime_context

CompletionApi = tuple[
    AsyncClient,
    async_sessionmaker[AsyncSession],
    FakeObjectStorage,
    FastAPI,
]
TRACE_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
async def completion_api(tmp_path: Path) -> AsyncIterator[CompletionApi]:
    database_path = (tmp_path / "completion.sqlite3").as_posix()
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
            "title": "완료 확인 테스트",
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
        next(link["secret"] for link in reversed(created["access_links"]) if link["role"] == role),
    )


def _participant_id(created: dict[str, Any], role: str) -> UUID:
    return UUID(
        next(
            participant["id"]
            for participant in created["job"]["participants"]
            if participant["role"] == role
        )
    )


def _headers(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


async def _locked_scope(client: AsyncClient, created: dict[str, Any]) -> str:
    job_id = created["job"]["id"]
    versions_url = f"/api/v1/move-jobs/{job_id}/scope-versions"
    version = await client.post(
        versions_url,
        headers=_headers(_secret(created, "customer")),
        json={
            "content": {
                "items": [
                    {
                        "item_key": "sofa",
                        "room_zone_id": created["job"]["locations"][0]["room_zones"][0]["id"],
                        "description": "소파 운반",
                    }
                ]
            }
        },
    )
    assert version.status_code == 201
    version_id = cast(str, version.json()["id"])
    approval_url = f"{versions_url}/{version_id}/approvals"
    for role in ("customer", "company_manager"):
        approval = await client.post(
            approval_url,
            headers=_headers(_secret(created, role)),
        )
        assert approval.status_code == 201
    return version_id


async def _upload_media(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    storage: FakeObjectStorage,
    created: dict[str, Any],
    *,
    role: str = "field_worker",
    purpose: str = "completion",
    complete: bool = True,
    generation: str | None = "7",
) -> str:
    job_id = created["job"]["id"]
    headers = _headers(_secret(created, role))
    capture = await client.post(
        f"/api/v1/move-jobs/{job_id}/capture-sessions",
        headers=headers,
    )
    assert capture.status_code == 201
    capture_id = capture.json()["id"]
    upload = await client.post(
        f"/api/v1/move-jobs/{job_id}/capture-sessions/{capture_id}/media-assets/upload",
        headers=headers,
        json={
            "room_zone_id": created["job"]["locations"][0]["room_zones"][0]["id"],
            "media_purpose": purpose,
            "content_type": "image/jpeg",
            "content_length": 14,
        },
    )
    assert upload.status_code == 201
    asset_id = cast(str, upload.json()["asset"]["id"])
    if not complete:
        return asset_id
    async with factory() as session:
        asset = await session.get(MediaAsset, UUID(asset_id))
        assert asset is not None
        object_key = asset.object_key
    storage.metadata[object_key] = StorageObjectMetadata(
        object_key=object_key,
        content_type="image/jpeg",
        size_bytes=14,
        sha256_hex="d" * 64,
        generation=generation,
    )
    completed = await client.post(
        f"/api/v1/move-jobs/{job_id}/capture-sessions/{capture_id}"
        f"/media-assets/{asset_id}/complete",
        headers=headers,
    )
    assert completed.status_code == 200
    return asset_id


@pytest.mark.anyio
async def test_bilateral_completion_updates_job_and_exposes_sanitized_audit(
    completion_api: CompletionApi,
) -> None:
    client, factory, storage, _ = completion_api
    created = await _create_job(client)
    job_id = created["job"]["id"]
    scope_version_id = await _locked_scope(client, created)
    evidence_id = await _upload_media(client, factory, storage, created)
    second_evidence_id = await _upload_media(
        client,
        factory,
        storage,
        created,
        generation="8",
    )
    confirmation_url = f"/api/v1/move-jobs/{job_id}/completion-confirmations"
    payload = {
        "scope_version_id": scope_version_id,
        "evidence_media_asset_ids": [evidence_id, second_evidence_id],
    }

    customer = await client.post(
        confirmation_url,
        headers=_headers(_secret(created, "customer")),
        json=payload,
    )
    assert customer.status_code == 201
    assert customer.json()["job_status"] == "draft"
    assert customer.json()["completed_at"] is None
    async with factory() as session:
        assert {row.job_type for row in (await session.scalars(select(BackgroundJob))).all()} == {
            BackgroundJobType.MEDIA_VALIDATION
        }
    duplicate_customer = await client.post(
        confirmation_url,
        headers=_headers(_secret(created, "customer")),
        json=payload,
    )
    assert duplicate_customer.status_code == 409

    for invalid_generation in (None, " "):
        async with factory.begin() as session:
            await session.execute(text("PRAGMA ignore_check_constraints = ON"))
            asset = await session.get(MediaAsset, UUID(evidence_id))
            assert asset is not None
            asset.generation = invalid_generation
            await session.flush()
            await session.execute(text("PRAGMA ignore_check_constraints = OFF"))
        blocked = await client.post(
            confirmation_url,
            headers=_headers(_secret(created, "company_manager")),
            json=payload,
        )
        assert blocked.status_code == 422
        async with factory() as session:
            job = await session.get(MoveJob, UUID(job_id))
            assert job is not None and job.status is MoveJobStatus.DRAFT
            assert {
                row.job_type for row in (await session.scalars(select(BackgroundJob))).all()
            } == {BackgroundJobType.MEDIA_VALIDATION}
    async with factory.begin() as session:
        asset = await session.get(MediaAsset, UUID(evidence_id))
        assert asset is not None
        asset.generation = "7"
    manager = await client.post(
        confirmation_url,
        headers=_headers(_secret(created, "company_manager")),
        json=payload,
    )
    assert manager.status_code == 201
    assert manager.json()["job_status"] == "completed"
    assert manager.json()["completed_at"] is not None

    loaded = await client.get(
        f"/api/v1/move-jobs/{job_id}",
        headers=_headers(_secret(created, "field_worker")),
    )
    assert loaded.json()["status"] == "completed"
    assert loaded.json()["completed_at"] is not None

    confirmations = await client.get(
        confirmation_url,
        headers=_headers(_secret(created, "field_worker")),
    )
    assert confirmations.status_code == 200
    assert [item["role"] for item in confirmations.json()] == [
        "customer",
        "company_manager",
    ]
    assert all(
        set(item["evidence_media_asset_ids"]) == {evidence_id, second_evidence_id}
        for item in confirmations.json()
    )

    completed_at = datetime.fromisoformat(manager.json()["completed_at"])
    async with factory.begin() as session:
        background_jobs = (
            await session.scalars(
                select(BackgroundJob).where(
                    BackgroundJob.job_type == BackgroundJobType.MEDIA_RETENTION_DELETE
                )
            )
        ).all()
        assert len(background_jobs) == 2
        jobs_by_asset = {row.media_asset_id: row for row in background_jobs}
        assert set(jobs_by_asset) == {UUID(evidence_id), UUID(second_evidence_id)}
        background_job = jobs_by_asset[UUID(evidence_id)]
        scheduled_at = background_job.scheduled_at
        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=UTC)
        assert {
            (row.status, row.target_generation, row.created_by_participant_id)
            for row in background_jobs
        } == {
            (BackgroundJobStatus.PENDING, "7", None),
            (BackgroundJobStatus.PENDING, "8", None),
        }
        assert all(
            (
                row.scheduled_at.replace(tzinfo=UTC)
                if row.scheduled_at.tzinfo is None
                else row.scheduled_at
            )
            == completed_at + timedelta(days=30)
            for row in background_jobs
        )
    manual = await client.post(
        f"/api/v1/move-jobs/{job_id}/background-jobs",
        headers=_headers(_secret(created, "company_manager")),
        json={"media_asset_id": evidence_id},
    )
    assert manual.status_code == 201
    assert manual.json()["id"] == str(background_job.id)
    assert datetime.fromisoformat(manual.json()["scheduled_at"]) == scheduled_at
    async with factory.begin() as session:
        early_claims = await claim_background_jobs(
            session,
            now=scheduled_at - timedelta(microseconds=1),
        )
        assert all(
            claim.task.job_type == BackgroundJobType.MEDIA_VALIDATION for claim in early_claims
        )
    async with factory.begin() as session:
        claims = await claim_background_jobs(session, now=scheduled_at)
        assert {
            UUID(str(claim.task.background_job_id))
            for claim in (*early_claims, *claims)
            if claim.task.job_type == BackgroundJobType.MEDIA_RETENTION_DELETE
        } == {row.id for row in background_jobs}

    audit = await client.get(
        f"/api/v1/move-jobs/{job_id}/audit-events",
        headers=_headers(_secret(created, "customer")),
    )
    assert audit.status_code == 200
    event_types = [event["event_type"] for event in audit.json()]
    assert event_types == [
        "job_created",
        "access_link_issued",
        "access_link_issued",
        "access_link_issued",
        "participant_connected",
        "scope_version_created",
        "scope_version_approved",
        "participant_connected",
        "scope_version_approved",
        "scope_version_locked",
        "participant_connected",
        "completion_media_uploaded",
        "completion_media_uploaded",
        "completion_confirmed",
        "completion_confirmed",
        "job_completed",
    ]
    serialized_audit = str(audit.json())
    assert _secret(created, "customer") not in serialized_audit
    assert "https://" not in serialized_audit
    assert "소파 운반" not in serialized_audit

    repeated = await client.post(
        confirmation_url,
        headers=_headers(_secret(created, "customer")),
        json=payload,
    )
    assert repeated.status_code == 409
    blocked_change = await client.post(
        f"/api/v1/move-jobs/{job_id}/change-requests",
        headers=_headers(_secret(created, "field_worker")),
        json={
            "base_scope_version_id": scope_version_id,
            "description": "완료 후 변경 시도",
            "proposed_content": {
                "items": [
                    {
                        "item_key": "sofa",
                        "room_zone_id": created["job"]["locations"][0]["room_zones"][0]["id"],
                        "description": "완료 후 변경",
                    }
                ]
            },
            "evidence_media_asset_ids": [evidence_id],
        },
    )
    assert blocked_change.status_code == 409
    async with factory() as session:
        assert len((await session.scalars(select(AuditEvent))).all()) == len(audit.json())


@pytest.mark.anyio
async def test_completion_rejects_role_scope_change_and_media_boundaries(
    completion_api: CompletionApi,
) -> None:
    client, factory, storage, application = completion_api
    created = await _create_job(client)
    job_id = created["job"]["id"]
    unlocked = await client.post(
        f"/api/v1/move-jobs/{job_id}/scope-versions",
        headers=_headers(_secret(created, "customer")),
        json={
            "content": {
                "items": [
                    {
                        "item_key": "sofa",
                        "room_zone_id": created["job"]["locations"][0]["room_zones"][0]["id"],
                        "description": "소파 운반",
                    }
                ]
            }
        },
    )
    evidence_id = await _upload_media(client, factory, storage, created)
    confirmation_url = f"/api/v1/move-jobs/{job_id}/completion-confirmations"
    unlocked_payload = {
        "scope_version_id": unlocked.json()["id"],
        "evidence_media_asset_ids": [evidence_id],
    }
    assert (
        await client.post(
            confirmation_url,
            headers=_headers(_secret(created, "field_worker")),
            json=unlocked_payload,
        )
    ).status_code == 403
    assert (
        await client.post(
            confirmation_url,
            headers=_headers(_secret(created, "customer")),
            json=unlocked_payload,
        )
    ).status_code == 409

    approval_url = f"/api/v1/move-jobs/{job_id}/scope-versions/{unlocked.json()['id']}/approvals"
    for role in ("customer", "company_manager"):
        await client.post(approval_url, headers=_headers(_secret(created, role)))
    locked_payload = unlocked_payload
    configured_context = application.state.runtime_context
    application.state.runtime_context = create_runtime_context(
        RuntimeKind.API,
        Settings(environment=AppEnvironment.TEST),
    )
    unavailable = await client.post(
        confirmation_url,
        headers=_headers(_secret(created, "customer")),
        json=locked_payload,
    )
    application.state.runtime_context = configured_context
    assert unavailable.status_code == 503
    async with factory.begin() as session:
        session.add(
            ScopeVersion(
                job_id=UUID(job_id),
                parent_version_id=UUID(unlocked.json()["id"]),
                sequence_number=2,
                content={
                    "schema_version": 1,
                    "items": [
                        {
                            "item_key": "sofa",
                            "room_zone_id": created["job"]["locations"][0]["room_zones"][0]["id"],
                            "description": "후속 범위",
                        }
                    ],
                },
                content_hash="e" * 64,
                created_by_participant_id=_participant_id(created, "customer"),
            )
        )
    past_version = await client.post(
        confirmation_url,
        headers=_headers(_secret(created, "customer")),
        json=locked_payload,
    )
    assert past_version.status_code == 409
    async with factory.begin() as session:
        await session.execute(
            delete(ScopeVersion).where(
                ScopeVersion.parent_version_id == UUID(unlocked.json()["id"])
            )
        )

    duplicate_payload = locked_payload | {"evidence_media_asset_ids": [evidence_id, evidence_id]}
    assert (
        await client.post(
            confirmation_url,
            headers=_headers(_secret(created, "customer")),
            json=duplicate_payload,
        )
    ).status_code == 422

    inventory_id = await _upload_media(
        client,
        factory,
        storage,
        created,
        purpose="inventory",
    )
    pending_id = await _upload_media(
        client,
        factory,
        storage,
        created,
        complete=False,
    )
    customer_media_id = await _upload_media(
        client,
        factory,
        storage,
        created,
        role="customer",
    )
    second_job = await _create_job(client)
    cross_job_id = await _upload_media(client, factory, storage, second_job)
    for invalid_id in (inventory_id, pending_id, customer_media_id, cross_job_id):
        invalid = await client.post(
            confirmation_url,
            headers=_headers(_secret(created, "customer")),
            json=locked_payload | {"evidence_media_asset_ids": [invalid_id]},
        )
        assert invalid.status_code == 422

    missing_scope = await client.post(
        confirmation_url,
        headers=_headers(_secret(created, "customer")),
        json=locked_payload | {"scope_version_id": str(uuid4())},
    )
    assert missing_scope.status_code == 404
    hidden = await client.get(
        f"/api/v1/move-jobs/{second_job['job']['id']}/audit-events",
        headers=_headers(_secret(created, "customer")),
    )
    assert hidden.status_code == 404

    first = await client.post(
        confirmation_url,
        headers=_headers(_secret(created, "customer")),
        json=locked_payload,
    )
    assert first.status_code == 201
    other_evidence_id = await _upload_media(client, factory, storage, created)
    mismatched = await client.post(
        confirmation_url,
        headers=_headers(_secret(created, "company_manager")),
        json=locked_payload | {"evidence_media_asset_ids": [other_evidence_id]},
    )
    assert mismatched.status_code == 409


@pytest.mark.anyio
async def test_completion_blocks_pending_change_and_freezes_new_change(
    completion_api: CompletionApi,
) -> None:
    client, factory, storage, _ = completion_api
    created = await _create_job(client)
    job_id = created["job"]["id"]
    scope_version_id = await _locked_scope(client, created)
    completion_evidence_id = await _upload_media(client, factory, storage, created)
    change_evidence_id = await _upload_media(
        client,
        factory,
        storage,
        created,
        purpose="change_evidence",
    )
    change_payload = {
        "base_scope_version_id": scope_version_id,
        "description": "현장 추가 포장",
        "proposed_content": {
            "items": [
                {
                    "item_key": "sofa",
                    "room_zone_id": created["job"]["locations"][0]["room_zones"][0]["id"],
                    "description": "소파 추가 포장",
                }
            ]
        },
        "evidence_media_asset_ids": [change_evidence_id],
    }
    changes_url = f"/api/v1/move-jobs/{job_id}/change-requests"
    pending = await client.post(
        changes_url,
        headers=_headers(_secret(created, "field_worker")),
        json=change_payload,
    )
    assert pending.status_code == 201
    confirmation_url = f"/api/v1/move-jobs/{job_id}/completion-confirmations"
    completion_payload = {
        "scope_version_id": scope_version_id,
        "evidence_media_asset_ids": [completion_evidence_id],
    }
    assert (
        await client.post(
            confirmation_url,
            headers=_headers(_secret(created, "customer")),
            json=completion_payload,
        )
    ).status_code == 409
    rejected = await client.post(
        f"{changes_url}/{pending.json()['id']}/decision",
        headers=_headers(_secret(created, "customer")),
        json={"decision": "reject", "note": "완료 범위 유지"},
    )
    assert rejected.status_code == 200
    assert (
        await client.post(
            confirmation_url,
            headers=_headers(_secret(created, "customer")),
            json=completion_payload,
        )
    ).status_code == 201
    frozen = await client.post(
        changes_url,
        headers=_headers(_secret(created, "field_worker")),
        json=change_payload,
    )
    assert frozen.status_code == 409

    unknown_job = await client.post(
        f"/api/v1/move-jobs/{uuid4()}/change-requests",
        headers=_headers(_secret(created, "field_worker")),
        json=change_payload,
    )
    assert unknown_job.status_code == 404


@pytest.mark.anyio
async def test_completion_service_maps_wrong_actor_canceled_job_and_database_race(
    completion_api: CompletionApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory, storage, _ = completion_api
    created = await _create_job(client)
    job_id = UUID(created["job"]["id"])
    scope_version_id = UUID(await _locked_scope(client, created))
    evidence_id = UUID(await _upload_media(client, factory, storage, created))
    command = CompletionConfirmationCreate(
        scope_version_id=scope_version_id,
        evidence_media_asset_ids=(evidence_id,),
    )
    async with factory.begin() as session:
        with pytest.raises(CompletionResourceNotFoundError):
            await confirm_completion(
                session,
                job_id,
                _participant_id(created, "field_worker"),
                ParticipantRole.FIELD_WORKER,
                command,
                retention_days=30,
                trace_id=TRACE_ID,
            )
        with pytest.raises(ScopeResourceNotFoundError):
            await create_change_request(
                session,
                uuid4(),
                _participant_id(created, "field_worker"),
                ChangeRequestCreate(
                    base_scope_version_id=scope_version_id,
                    description="없는 작업",
                    proposed_content=ScopeContent.model_validate(
                        {
                            "items": [
                                {
                                    "item_key": "sofa",
                                    "room_zone_id": created["job"]["locations"][0]["room_zones"][0][
                                        "id"
                                    ],
                                    "description": "없는 작업",
                                }
                            ]
                        },
                        strict=False,
                    ),
                    evidence_media_asset_ids=(evidence_id,),
                ),
            )

    async with factory.begin() as session:
        with pytest.raises(CompletionResourceNotFoundError):
            await confirm_completion(
                session,
                uuid4(),
                _participant_id(created, "customer"),
                ParticipantRole.CUSTOMER,
                command,
                retention_days=30,
                trace_id=TRACE_ID,
            )

    async with factory.begin() as session:
        await session.execute(
            update(MoveJob).where(MoveJob.id == job_id).values(status=MoveJobStatus.CANCELED)
        )
    response = await client.post(
        f"/api/v1/move-jobs/{job_id}/completion-confirmations",
        headers=_headers(_secret(created, "customer")),
        json=command.model_dump(mode="json"),
    )
    assert response.status_code == 409

    async with factory.begin() as session:
        await session.execute(
            update(MoveJob).where(MoveJob.id == job_id).values(status=MoveJobStatus.DRAFT)
        )
    second_created = await _create_job(client)
    second_scope_version_id = UUID(await _locked_scope(client, second_created))
    async with factory.begin() as session:
        session.add(
            CompletionConfirmation(
                job_id=job_id,
                scope_version_id=second_scope_version_id,
                participant_id=_participant_id(created, "company_manager"),
                role=ParticipantRole.COMPANY_MANAGER,
            )
        )
    async with factory.begin() as session:
        with pytest.raises(CompletionConflictError):
            await confirm_completion(
                session,
                job_id,
                _participant_id(created, "customer"),
                ParticipantRole.CUSTOMER,
                command,
                retention_days=30,
                trace_id=TRACE_ID,
            )
        await session.execute(
            delete(CompletionConfirmation).where(CompletionConfirmation.job_id == job_id)
        )

    async with factory.begin() as session:
        job = await session.get(MoveJob, job_id)
        assert job is not None
        job.status = MoveJobStatus.COMPLETED
        job.completed_at = job.created_at
    response = await client.post(
        f"/api/v1/move-jobs/{job_id}/completion-confirmations",
        headers=_headers(_secret(created, "customer")),
        json=command.model_dump(mode="json"),
    )
    assert response.status_code == 409
    async with factory.begin() as session:
        job = await session.get(MoveJob, job_id)
        assert job is not None
        job.status = MoveJobStatus.DRAFT
        job.completed_at = None

    async with factory() as session, session.begin():
        original_flush = session.flush
        flush_calls = 0

        async def fail_second_flush(objects: Sequence[object] | None = None) -> None:
            nonlocal flush_calls
            flush_calls += 1
            if flush_calls == 2:
                raise IntegrityError("duplicate completion", {}, RuntimeError("duplicate"))
            await original_flush(objects)

        monkeypatch.setattr(session, "flush", fail_second_flush)
        with pytest.raises(CompletionConflictError):
            await confirm_completion(
                session,
                job_id,
                _participant_id(created, "customer"),
                ParticipantRole.CUSTOMER,
                command,
                retention_days=30,
                trace_id=TRACE_ID,
            )


@pytest.mark.anyio
async def test_audit_tracks_access_and_change_lifecycle_without_sensitive_text(
    completion_api: CompletionApi,
) -> None:
    client, factory, storage, _ = completion_api
    created_response = await client.post(
        "/api/v1/move-jobs",
        json={
            "title": "감사 이력 테스트",
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
    assert created_response.status_code == 201
    created = cast(dict[str, Any], created_response.json())
    job_id = created["job"]["id"]
    manager_headers = _headers(_secret(created, "company_manager"))
    worker_id = next(
        participant["id"]
        for participant in created["job"]["participants"]
        if participant["role"] == "field_worker"
    )
    rotated = await client.post(
        f"/api/v1/move-jobs/{job_id}/participants/{worker_id}/access-links",
        headers=_headers(_secret(created, "field_worker")),
    )
    assert rotated.status_code == 201
    created["access_links"].append(rotated.json())
    worker_secret = _secret(created, "field_worker")

    scope_version_id = await _locked_scope(client, created)
    evidence_id = await _upload_media(
        client,
        factory,
        storage,
        created,
        purpose="change_evidence",
    )
    changes_url = f"/api/v1/move-jobs/{job_id}/change-requests"
    change = await client.post(
        changes_url,
        headers=_headers(worker_secret),
        json={
            "base_scope_version_id": scope_version_id,
            "description": "SECRET_DESCRIPTION",
            "proposed_content": {
                "items": [
                    {
                        "item_key": "sofa",
                        "room_zone_id": created["job"]["locations"][0]["room_zones"][0]["id"],
                        "description": "SECRET_SCOPE_TEXT",
                    }
                ]
            },
            "evidence_media_asset_ids": [evidence_id],
        },
    )
    assert change.status_code == 201
    change_id = change.json()["id"]
    assert (
        await client.post(
            f"{changes_url}/{change_id}/clarification",
            headers=manager_headers,
            json={"message": "SECRET_QUESTION"},
        )
    ).status_code == 200
    assert (
        await client.post(
            f"{changes_url}/{change_id}/explanation",
            headers=_headers(worker_secret),
            json={"explanation": "SECRET_EXPLANATION"},
        )
    ).status_code == 200
    assert (
        await client.post(
            f"{changes_url}/{change_id}/decision",
            headers=manager_headers,
            json={"decision": "reject", "note": "SECRET_DECISION_NOTE"},
        )
    ).status_code == 200

    access_link_id = rotated.json()["id"]
    revoke_url = f"/api/v1/move-jobs/{job_id}/access-links/{access_link_id}/revoke"
    assert (await client.post(revoke_url, headers=manager_headers)).status_code == 204
    assert (await client.post(revoke_url, headers=manager_headers)).status_code == 204

    audit = await client.get(
        f"/api/v1/move-jobs/{job_id}/audit-events",
        headers=manager_headers,
    )
    assert audit.status_code == 200
    event_types = [event["event_type"] for event in audit.json()]
    for expected in (
        "access_link_issued",
        "access_link_revoked",
        "change_requested",
        "change_clarification_requested",
        "change_explained",
        "change_rejected",
    ):
        assert expected in event_types
    assert event_types.count("access_link_revoked") == 1

    serialized = str(audit.json())
    for sensitive in (
        worker_secret,
        "SECRET_DESCRIPTION",
        "SECRET_SCOPE_TEXT",
        "SECRET_QUESTION",
        "SECRET_EXPLANATION",
        "SECRET_DECISION_NOTE",
    ):
        assert sensitive not in serialized
