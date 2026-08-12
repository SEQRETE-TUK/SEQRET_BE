"""Field change evidence, decision workflow, and result-version tests."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import AppEnvironment, Settings
from app.contracts.fakes import FakeObjectStorage
from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.ports import ProviderError, ProviderErrorKind, StorageObjectMetadata
from app.main import create_app
from app.modules.capture.models import MediaAsset
from app.modules.scope.schemas import (
    ChangeDecisionCreate,
    ChangeEvidenceReadResponse,
    ChangeRequestCreate,
    ScopeContent,
)
from app.modules.scope.service import (
    ScopeResourceNotFoundError,
    create_change_request,
    decide_change_request,
    explain_change_request,
    list_change_requests,
    request_change_clarification,
)
from app.platform.db import Base, create_session_factory

ChangeApi = tuple[AsyncClient, async_sessionmaker[AsyncSession], FakeObjectStorage]


@pytest.fixture
async def change_api(tmp_path: Path) -> AsyncIterator[ChangeApi]:
    database_path = (tmp_path / "change.sqlite3").as_posix()
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
        yield client, factory, storage
    await engine.dispose()


async def _create_job(client: AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/move-jobs",
        json={
            "title": "현장 변경 테스트",
            "participants": [
                {"role": "customer", "display_name": "고객"},
                {"role": "company_manager", "display_name": "관리자"},
                {"role": "field_worker", "display_name": "현장 작업자"},
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


def _scope_content(created: dict[str, Any], description: str) -> dict[str, Any]:
    zone_id = created["job"]["locations"][0]["room_zones"][0]["id"]
    return {
        "schema_version": 1,
        "items": [
            {
                "item_key": "sofa",
                "room_zone_id": zone_id,
                "description": description,
            }
        ],
    }


async def _create_scope(
    client: AsyncClient,
    created: dict[str, Any],
    *,
    lock: bool,
) -> dict[str, Any]:
    job_id = created["job"]["id"]
    versions_url = f"/api/v1/move-jobs/{job_id}/scope-versions"
    response = await client.post(
        versions_url,
        headers=_headers(_secret(created, "customer")),
        json={"content": _scope_content(created, "소파 이동")},
    )
    assert response.status_code == 201
    version = cast(dict[str, Any], response.json())
    if lock:
        approval_url = f"{versions_url}/{version['id']}/approvals"
        assert (
            await client.post(
                approval_url,
                headers=_headers(_secret(created, "customer")),
            )
        ).status_code == 201
        manager = await client.post(
            approval_url,
            headers=_headers(_secret(created, "company_manager")),
        )
        assert manager.status_code == 201
        version = cast(dict[str, Any], manager.json()["version"])
    return version


async def _upload_evidence(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    storage: FakeObjectStorage,
    created: dict[str, Any],
    *,
    role: str = "field_worker",
    purpose: str = "change_evidence",
    complete: bool = True,
) -> str:
    job_id = created["job"]["id"]
    secret = _secret(created, role)
    capture = await client.post(
        f"/api/v1/move-jobs/{job_id}/capture-sessions",
        headers=_headers(secret),
    )
    assert capture.status_code == 201
    capture_id = capture.json()["id"]
    zone_id = created["job"]["locations"][0]["room_zones"][0]["id"]
    upload = await client.post(
        f"/api/v1/move-jobs/{job_id}/capture-sessions/{capture_id}/media-assets/upload",
        headers=_headers(secret),
        json={
            "room_zone_id": zone_id,
            "media_purpose": purpose,
            "content_type": "image/jpeg",
            "content_length": 12,
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
        size_bytes=12,
        sha256_hex="c" * 64,
        generation="7",
    )
    completed = await client.post(
        f"/api/v1/move-jobs/{job_id}/capture-sessions/{capture_id}"
        f"/media-assets/{asset_id}/complete",
        headers=_headers(secret),
    )
    assert completed.status_code == 200
    return asset_id


def _change_payload(
    created: dict[str, Any],
    base_version_id: str,
    evidence_id: str,
    *,
    description: str = "소파 포장 추가",
) -> dict[str, Any]:
    return {
        "base_scope_version_id": base_version_id,
        "description": "현장에서 포장이 추가로 필요함",
        "proposed_content": _scope_content(created, description),
        "evidence_media_asset_ids": [evidence_id],
    }


@pytest.mark.anyio
async def test_change_request_clarification_approval_and_result_confirmation(
    change_api: ChangeApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory, storage = change_api
    created = await _create_job(client)
    base = await _create_scope(client, created, lock=True)
    evidence_id = await _upload_evidence(client, factory, storage, created)
    job_id = created["job"]["id"]
    changes_url = f"/api/v1/move-jobs/{job_id}/change-requests"
    worker_headers = _headers(_secret(created, "field_worker"))
    customer_headers = _headers(_secret(created, "customer"))
    manager_headers = _headers(_secret(created, "company_manager"))

    created_request = await client.post(
        changes_url,
        headers=worker_headers,
        json=_change_payload(created, base["id"], evidence_id),
    )
    assert created_request.status_code == 201
    body = created_request.json()
    assert body["status"] == "pending"
    assert body["evidence_media_asset_ids"] == [evidence_id]
    assert body["result_scope_version_id"] is None
    change_id = body["id"]

    read_requests: list[tuple[str, str, int, float]] = []
    original_create_read_url = storage.create_read_url
    signed_read_url = (
        "https://STORAGE.INVALID:443/read/jobs/%2F?X-Goog-Signature=A%2B&X-Goog-Credential=jobs%2F1"
    )

    async def record_read_url(
        *,
        object_key: str,
        generation: str,
        expires_in_seconds: int,
        timeout_seconds: float,
    ) -> str:
        read_requests.append((object_key, generation, expires_in_seconds, timeout_seconds))
        await original_create_read_url(
            object_key=object_key,
            generation=generation,
            expires_in_seconds=expires_in_seconds,
            timeout_seconds=timeout_seconds,
        )
        return signed_read_url

    monkeypatch.setattr(storage, "create_read_url", record_read_url)
    evidence_url = f"{changes_url}/{change_id}/evidence/{evidence_id}/read-url"
    async with factory.begin() as session:
        asset = await session.get(MediaAsset, UUID(evidence_id))
        assert asset is not None
        asset.status = MediaAssetStatus.READY
    for headers in (customer_headers, manager_headers):
        readable = await client.get(evidence_url, headers=headers)
        assert readable.status_code == 200
        assert readable.json()["media_asset_id"] == evidence_id
        assert readable.json()["read_url"] == signed_read_url
        assert readable.json()["expires_at"] is not None
        assert readable.headers["cache-control"] == "no-store"
        parsed = ChangeEvidenceReadResponse.model_validate(readable.json(), strict=False)
        assert "read_url=" not in repr(parsed)
        assert 290 <= (parsed.expires_at - datetime.now(UTC)).total_seconds() <= 300
    assert [
        (generation, expires, timeout) for _, generation, expires, timeout in read_requests
    ] == [
        ("7", 300, 5.0),
        ("7", 300, 5.0),
    ]
    openapi = (await client.get("/openapi.json")).json()
    route_responses = openapi["paths"][
        evidence_url.replace(job_id, "{job_id}")
        .replace(change_id, "{change_request_id}")
        .replace(evidence_id, "{media_asset_id}")
    ]["get"]["responses"]
    assert {"200", "401", "403", "404", "409", "503"} <= set(route_responses)
    read_url_schema = openapi["components"]["schemas"]["ChangeEvidenceReadResponse"]
    assert read_url_schema["properties"]["read_url"]["format"] == "uri"
    assert (await client.get(evidence_url, headers=worker_headers)).status_code == 403

    listed = await client.get(changes_url, headers=customer_headers)
    assert listed.status_code == 200
    assert listed.json() == [body]

    clarification_url = f"{changes_url}/{change_id}/clarification"
    clarification = await client.post(
        clarification_url,
        headers=manager_headers,
        json={"message": "추가 포장 사유를 알려주세요"},
    )
    assert clarification.status_code == 200
    assert clarification.json()["status"] == "clarification_requested"
    assert clarification.json()["clarification_requested_by_participant_id"] == str(
        _participant_id(created, "company_manager")
    )
    assert (
        await client.post(
            clarification_url,
            headers=customer_headers,
            json={"message": "다시 질문"},
        )
    ).status_code == 409

    explanation_url = f"{changes_url}/{change_id}/explanation"
    assert (
        await client.post(
            explanation_url,
            headers=manager_headers,
            json={"explanation": "권한 없음"},
        )
    ).status_code == 403
    explanation = await client.post(
        explanation_url,
        headers=worker_headers,
        json={"explanation": "운송 중 손상 방지를 위한 포장입니다"},
    )
    assert explanation.status_code == 200
    assert explanation.json()["status"] == "pending"
    assert explanation.json()["explained_at"] is not None
    assert (
        await client.post(
            explanation_url,
            headers=worker_headers,
            json={"explanation": "중복 설명"},
        )
    ).status_code == 409

    decision_url = f"{changes_url}/{change_id}/decision"
    assert (
        await client.post(
            decision_url,
            headers=worker_headers,
            json={"decision": "approve"},
        )
    ).status_code == 403
    approved = await client.post(
        decision_url,
        headers=customer_headers,
        json={"decision": "approve", "note": "현장 증거 확인"},
    )
    assert approved.status_code == 200
    approved_body = approved.json()
    assert approved_body["status"] == "approved"
    assert approved_body["decided_at"] is not None
    result_version_id = approved_body["result_scope_version_id"]
    assert result_version_id is not None
    assert (
        await client.post(
            decision_url,
            headers=manager_headers,
            json={"decision": "reject", "note": "늦은 결정"},
        )
    ).status_code == 409
    new_request_on_past_base = await client.post(
        changes_url,
        headers=worker_headers,
        json=_change_payload(
            created,
            base["id"],
            evidence_id,
            description="이미 지난 기준 범위",
        ),
    )
    assert new_request_on_past_base.status_code == 409

    versions_url = f"/api/v1/move-jobs/{job_id}/scope-versions"
    versions = await client.get(versions_url, headers=customer_headers)
    assert versions.status_code == 200
    assert [version["sequence_number"] for version in versions.json()] == [1, 2]
    result_version = versions.json()[1]
    assert result_version["id"] == result_version_id
    assert result_version["parent_version_id"] == base["id"]
    assert result_version["locked_at"] is None
    assert result_version["content"]["items"][0]["description"] == "소파 포장 추가"

    direct_edit = await client.post(
        versions_url,
        headers=customer_headers,
        json={
            "parent_version_id": result_version_id,
            "content": _scope_content(created, "연결을 우회한 편집"),
        },
    )
    assert direct_edit.status_code == 409

    approval_url = f"{versions_url}/{result_version_id}/approvals"
    assert (await client.post(approval_url, headers=customer_headers)).status_code == 201
    locked = await client.post(approval_url, headers=manager_headers)
    assert locked.status_code == 201
    assert locked.json()["version"]["locked_at"] is not None


@pytest.mark.anyio
async def test_rejected_change_never_creates_scope_version(change_api: ChangeApi) -> None:
    client, factory, storage = change_api
    created = await _create_job(client)
    base = await _create_scope(client, created, lock=True)
    evidence_id = await _upload_evidence(client, factory, storage, created)
    job_id = created["job"]["id"]
    changes_url = f"/api/v1/move-jobs/{job_id}/change-requests"
    request = await client.post(
        changes_url,
        headers=_headers(_secret(created, "field_worker")),
        json=_change_payload(created, base["id"], evidence_id),
    )
    decision_url = f"{changes_url}/{request.json()['id']}/decision"
    manager_headers = _headers(_secret(created, "company_manager"))

    assert (
        await client.post(
            decision_url,
            headers=manager_headers,
            json={"decision": "reject"},
        )
    ).status_code == 422
    rejected = await client.post(
        decision_url,
        headers=manager_headers,
        json={"decision": "reject", "note": "근거가 변경안을 뒷받침하지 않음"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["result_scope_version_id"] is None
    assert rejected.json()["decision_note"] == "근거가 변경안을 뒷받침하지 않음"

    versions = await client.get(
        f"/api/v1/move-jobs/{job_id}/scope-versions",
        headers=manager_headers,
    )
    assert len(versions.json()) == 1


@pytest.mark.anyio
async def test_change_request_rejects_unconfirmed_scope_and_invalid_evidence(
    change_api: ChangeApi,
) -> None:
    client, factory, storage = change_api
    created = await _create_job(client)
    unlocked = await _create_scope(client, created, lock=False)
    evidence_id = await _upload_evidence(client, factory, storage, created)
    job_id = created["job"]["id"]
    changes_url = f"/api/v1/move-jobs/{job_id}/change-requests"
    worker_headers = _headers(_secret(created, "field_worker"))
    customer_headers = _headers(_secret(created, "customer"))

    assert (
        await client.post(
            changes_url,
            headers=worker_headers,
            json=_change_payload(created, unlocked["id"], evidence_id),
        )
    ).status_code == 409
    approval_url = f"/api/v1/move-jobs/{job_id}/scope-versions/{unlocked['id']}/approvals"
    await client.post(approval_url, headers=customer_headers)
    await client.post(
        approval_url,
        headers=_headers(_secret(created, "company_manager")),
    )

    assert (
        await client.post(
            changes_url,
            headers=customer_headers,
            json=_change_payload(created, unlocked["id"], evidence_id),
        )
    ).status_code == 403
    assert (
        await client.post(
            changes_url,
            headers=worker_headers,
            json=_change_payload(created, unlocked["id"], evidence_id)
            | {"evidence_media_asset_ids": [evidence_id, evidence_id]},
        )
    ).status_code == 422
    assert (
        await client.post(
            changes_url,
            headers=worker_headers,
            json=_change_payload(created, unlocked["id"], evidence_id, description="소파 이동"),
        )
    ).status_code == 422

    inventory_id = await _upload_evidence(
        client,
        factory,
        storage,
        created,
        purpose="inventory",
    )
    pending_id = await _upload_evidence(
        client,
        factory,
        storage,
        created,
        complete=False,
    )
    customer_evidence_id = await _upload_evidence(
        client,
        factory,
        storage,
        created,
        role="customer",
    )
    second_job = await _create_job(client)
    cross_job_evidence_id = await _upload_evidence(
        client,
        factory,
        storage,
        second_job,
    )
    for invalid_id in (inventory_id, pending_id, customer_evidence_id, cross_job_evidence_id):
        invalid = await client.post(
            changes_url,
            headers=worker_headers,
            json=_change_payload(created, unlocked["id"], invalid_id),
        )
        assert invalid.status_code == 422

    missing_base = await client.post(
        changes_url,
        headers=worker_headers,
        json=_change_payload(created, str(uuid4()), evidence_id),
    )
    assert missing_base.status_code == 404
    missing_change = await client.post(
        f"{changes_url}/{uuid4()}/decision",
        headers=customer_headers,
        json={"decision": "approve"},
    )
    assert missing_change.status_code == 404

    hidden = await client.get(
        f"/api/v1/move-jobs/{second_job['job']['id']}/change-requests",
        headers=worker_headers,
    )
    assert hidden.status_code == 404


@pytest.mark.anyio
async def test_change_evidence_read_url_hides_resources_and_rejects_unreadable_media(
    change_api: ChangeApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory, storage = change_api
    created = await _create_job(client)
    base = await _create_scope(client, created, lock=True)
    evidence_id = await _upload_evidence(client, factory, storage, created)
    job_id = created["job"]["id"]
    changes_url = f"/api/v1/move-jobs/{job_id}/change-requests"
    request = await client.post(
        changes_url,
        headers=_headers(_secret(created, "field_worker")),
        json=_change_payload(created, base["id"], evidence_id),
    )
    read_url = f"{changes_url}/{request.json()['id']}/evidence/{evidence_id}/read-url"
    customer_headers = _headers(_secret(created, "customer"))

    unattached_id = await _upload_evidence(client, factory, storage, created)
    assert (
        await client.get(read_url.replace(evidence_id, unattached_id), headers=customer_headers)
    ).status_code == 404
    second_job = await _create_job(client)
    cross_job_url = read_url.replace(job_id, second_job["job"]["id"], 1)
    assert (
        await client.get(
            cross_job_url,
            headers=_headers(_secret(second_job, "customer")),
        )
    ).status_code == 404

    async with factory.begin() as session:
        asset = await session.get(MediaAsset, UUID(evidence_id))
        assert asset is not None
        asset.media_purpose = MediaPurpose.INVENTORY
    assert (await client.get(read_url, headers=customer_headers)).status_code == 404

    for unreadable_status in set(MediaAssetStatus) - {MediaAssetStatus.READY}:
        async with factory.begin() as session:
            asset = await session.get(MediaAsset, UUID(evidence_id))
            assert asset is not None
            asset.media_purpose = MediaPurpose.CHANGE_EVIDENCE
            asset.status = unreadable_status
        assert (await client.get(read_url, headers=customer_headers)).status_code == 409

    for invalid_generation in (None, " "):
        async with factory.begin() as session:
            asset = await session.get(MediaAsset, UUID(evidence_id))
            assert asset is not None
            asset.status = MediaAssetStatus.READY
            asset.generation = invalid_generation
        assert (await client.get(read_url, headers=customer_headers)).status_code == 409

    async with factory.begin() as session:
        asset = await session.get(MediaAsset, UUID(evidence_id))
        assert asset is not None
        asset.generation = "7"

    async def unavailable(**_kwargs: object) -> str:
        raise ProviderError(
            ProviderErrorKind.UNAVAILABLE,
            "provider detail must not leak",
            retryable=True,
        )

    monkeypatch.setattr(storage, "create_read_url", unavailable)
    provider_failure = await client.get(read_url, headers=customer_headers)
    assert provider_failure.status_code == 503
    assert provider_failure.json()["detail"] == "storage is unavailable"

    for invalid_read_url in (
        "http://storage.invalid/read?signature=must-not-leak",
        "https:///read?signature=must-not-leak",
        " https://storage.invalid/read?signature=must-not-leak ",
        "https://storage.invalid:not-a-port/read?signature=must-not-leak",
        "https://storage.invalid:70000/read?signature=must-not-leak",
    ):

        async def invalid_url(
            returned_url: str = invalid_read_url,
            **_kwargs: object,
        ) -> str:
            return returned_url

        monkeypatch.setattr(storage, "create_read_url", invalid_url)
        invalid_provider_url = await client.get(read_url, headers=customer_headers)
        assert invalid_provider_url.status_code == 503
        assert "must-not-leak" not in invalid_provider_url.text

    def missing_storage(*_args: object) -> None:
        raise HTTPException(status_code=503, detail="storage is unavailable")

    monkeypatch.setattr("app.modules.scope.router.get_storage_port", missing_storage)
    assert (
        await client.get(
            read_url,
            headers=_headers(_secret(created, "field_worker")),
        )
    ).status_code == 403
    assert (
        await client.get(
            read_url,
            headers=_headers(_secret(second_job, "customer")),
        )
    ).status_code == 404
    assert (await client.get(read_url, headers=customer_headers)).status_code == 503


@pytest.mark.anyio
async def test_change_service_rejects_wrong_roles_and_competing_approval(
    change_api: ChangeApi,
) -> None:
    client, factory, storage = change_api
    created = await _create_job(client)
    base = await _create_scope(client, created, lock=True)
    evidence_id = await _upload_evidence(client, factory, storage, created)
    job_id = created["job"]["id"]
    changes_url = f"/api/v1/move-jobs/{job_id}/change-requests"
    worker_headers = _headers(_secret(created, "field_worker"))
    first = await client.post(
        changes_url,
        headers=worker_headers,
        json=_change_payload(created, base["id"], evidence_id),
    )
    second = await client.post(
        changes_url,
        headers=worker_headers,
        json=_change_payload(
            created,
            base["id"],
            evidence_id,
            description="소파 이중 포장 추가",
        ),
    )
    worker_id = _participant_id(created, "field_worker")
    customer_id = _participant_id(created, "customer")

    async with factory.begin() as session:
        with pytest.raises(ScopeResourceNotFoundError):
            await create_change_request(
                session,
                UUID(job_id),
                customer_id,
                ChangeRequestCreate(
                    base_scope_version_id=UUID(base["id"]),
                    description="잘못된 역할",
                    proposed_content=ScopeContent.model_validate(
                        _scope_content(created, "잘못된 역할"),
                        strict=False,
                    ),
                    evidence_media_asset_ids=(UUID(evidence_id),),
                ),
            )
        with pytest.raises(ScopeResourceNotFoundError):
            await request_change_clarification(
                session,
                UUID(job_id),
                UUID(first.json()["id"]),
                worker_id,
                "잘못된 역할",
            )
        with pytest.raises(ScopeResourceNotFoundError):
            await explain_change_request(
                session,
                UUID(job_id),
                UUID(first.json()["id"]),
                customer_id,
                "잘못된 역할",
            )
        with pytest.raises(ScopeResourceNotFoundError):
            await decide_change_request(
                session,
                UUID(job_id),
                UUID(first.json()["id"]),
                worker_id,
                ChangeDecisionCreate(decision="approve"),
            )

    decision_headers = _headers(_secret(created, "customer"))
    assert (
        await client.post(
            f"{changes_url}/{first.json()['id']}/decision",
            headers=decision_headers,
            json={"decision": "approve"},
        )
    ).status_code == 200
    competing = await client.post(
        f"{changes_url}/{second.json()['id']}/decision",
        headers=decision_headers,
        json={"decision": "approve"},
    )
    assert competing.status_code == 409

    async with factory() as session:
        pending_asset = await session.get(MediaAsset, UUID(evidence_id))
        assert pending_asset is not None
        assert pending_asset.status is MediaAssetStatus.UPLOADED


@pytest.mark.anyio
async def test_change_request_list_uses_two_selects_for_many_requests(
    change_api: ChangeApi,
) -> None:
    client, factory, storage = change_api
    created = await _create_job(client)
    base = await _create_scope(client, created, lock=True)
    evidence_id = await _upload_evidence(client, factory, storage, created)
    job_id = created["job"]["id"]
    for index in range(3):
        response = await client.post(
            f"/api/v1/move-jobs/{job_id}/change-requests",
            headers=_headers(_secret(created, "field_worker")),
            json=_change_payload(
                created,
                base["id"],
                evidence_id,
                description=f"change {index}",
            ),
        )
        assert response.status_code == 201

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

    engine = factory.kw["bind"]
    event.listen(engine.sync_engine, "before_cursor_execute", count_selects)
    try:
        async with factory() as session:
            responses = await list_change_requests(session, UUID(job_id))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count_selects)

    assert len(responses) == 3
    assert all(response.evidence_media_asset_ids == (UUID(evidence_id),) for response in responses)
    assert selects == 2
