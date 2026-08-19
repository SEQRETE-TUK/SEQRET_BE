"""INT-03 field issue, quote proposal, and locked-result workflow tests."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
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
from app.contracts.fakes import FakeObjectStorage
from app.contracts.media import MediaAssetStatus
from app.contracts.ports import ProviderError, ProviderErrorKind
from app.main import create_app
from app.modules.capture.models import MediaAsset
from app.modules.field_change import service as field_change_service
from app.modules.field_change.models import (
    ChangeProposalDetail,
    FieldIssue,
    FieldIssueType,
)
from app.modules.field_change.schemas import (
    ChangeProposalCreate,
    ChangeProposalDecisionCreate,
    FieldIssueCreate,
)
from app.modules.field_change.service import (
    FieldChangeConflictError,
    FieldChangeNotFoundError,
    create_change_proposal,
    create_field_issue,
)
from app.modules.move_job.models import JobParticipant, MoveJob, MoveJobStatus
from app.modules.scope.models import (
    ChangeRequest,
    ChangeRequestStatus,
    ScopeApproval,
    ScopeVersion,
)
from app.modules.scope.service import ScopeResourceNotFoundError
from app.platform.db import Base, create_session_factory

FieldChangeApi = tuple[
    AsyncClient,
    async_sessionmaker[AsyncSession],
    FakeObjectStorage,
]


@pytest.fixture
async def field_change_api(tmp_path: Path) -> AsyncIterator[FieldChangeApi]:
    database_path = (tmp_path / "field-change.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        poolclass=NullPool,
    )
    factory = create_session_factory(engine)
    storage = FakeObjectStorage()
    application = create_app(Settings(environment=AppEnvironment.TEST, media_retention_days=30))
    application.state.database_session_factory = factory
    application.state.storage_port = storage
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        yield client, factory, storage
    await engine.dispose()


async def _create_job(client: AsyncClient, title: str = "INT-03 테스트") -> dict[str, Any]:
    response = await client.post(
        "/api/v1/move-jobs",
        json={
            "title": title,
            "scheduled_at": "2026-09-12T08:00:00+09:00",
            "participants": [
                {"role": "customer", "display_name": "합성 고객"},
                {"role": "company_manager", "display_name": "합성 업체"},
                {"role": "field_worker", "display_name": "합성 기사"},
            ],
            "locations": [
                {
                    "kind": "origin",
                    "label": "합성 출발지",
                    "room_zones": [{"name": "거실", "sort_order": 0}],
                },
                {
                    "kind": "destination",
                    "label": "합성 도착지",
                    "room_zones": [{"name": "거실", "sort_order": 0}],
                },
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


def _headers(created: dict[str, Any], role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_secret(created, role)}"}


def _content(created: dict[str, Any], description: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "items": [
            {
                "item_key": "sofa",
                "room_zone_id": created["job"]["locations"][0]["room_zones"][0]["id"],
                "description": description,
            }
        ],
    }


async def _confirmed_scope(
    client: AsyncClient,
    created: dict[str, Any],
    *,
    total_amount: int = 1_280_000,
) -> dict[str, Any]:
    job_id = created["job"]["id"]
    root = await client.post(
        f"/api/v1/move-jobs/{job_id}/scope-versions",
        headers=_headers(created, "customer"),
        json={"content": _content(created, "소파 일반 운반")},
    )
    assert root.status_code == 201
    proposal = await client.post(
        f"/api/v1/move-jobs/{job_id}/scope-proposals",
        headers=_headers(created, "company_manager"),
        json={
            "source_scope_version_id": root.json()["id"],
            "content": _content(created, "소파 일반 포장과 운반"),
            "quote": {
                "base_amount_krw": total_amount,
                "adjustments": [],
                "total_amount_krw": total_amount,
            },
            "execution_plan": {
                "vehicle_count": 1,
                "vehicle_description": "1톤 탑차",
                "worker_count": 2,
                "estimated_duration_minutes": 180,
                "notes": None,
            },
            "included_works": ["포장", "운반"],
            "exclusions": ["에어컨 이전"],
            "reason": "합성 기준 견적",
        },
    )
    assert proposal.status_code == 201
    confirmed = await client.post(
        f"/api/v1/move-jobs/{job_id}/scope-review/confirm",
        headers=_headers(created, "customer"),
        json={"scope_version_id": proposal.json()["result_scope_version_id"]},
    )
    assert confirmed.status_code == 200
    return cast(dict[str, Any], proposal.json())


async def _ready_evidence(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    created: dict[str, Any],
    *,
    ready: bool = True,
    role: str = "field_worker",
) -> str:
    job_id = created["job"]["id"]
    capture = await client.post(
        f"/api/v1/move-jobs/{job_id}/capture-sessions",
        headers=_headers(created, role),
        json={
            "consent_policy_version": "2026-08-17.v1",
            "privacy_notice_acknowledged": True,
        },
    )
    assert capture.status_code == 201
    upload = await client.post(
        f"/api/v1/move-jobs/{job_id}/capture-sessions/{capture.json()['id']}/media-assets/upload",
        headers=_headers(created, role),
        json={
            "room_zone_id": created["job"]["locations"][0]["room_zones"][0]["id"],
            "media_purpose": "change_evidence",
            "content_type": "image/jpeg",
            "content_length": 12,
        },
    )
    assert upload.status_code == 201
    media_id = cast(str, upload.json()["asset"]["id"])
    async with factory.begin() as session:
        asset = await session.get(MediaAsset, UUID(media_id))
        assert asset is not None
        asset.status = MediaAssetStatus.READY if ready else MediaAssetStatus.UPLOADED
        asset.actual_size_bytes = 12
        asset.sha256_hex = "a" * 64
        asset.generation = "7"
        asset.uploaded_at = datetime.now(UTC)
    return media_id


def _issue_payload(created: dict[str, Any], base_id: str, media_id: str) -> dict[str, Any]:
    return {
        "client_reference": str(uuid4()),
        "base_scope_version_id": base_id,
        "issue_type": "site_blocker",
        "title": "도착지 엘리베이터 고장",
        "description": "5층 창측 진입이 가능하고 사다리차 검토가 필요합니다",
        "evidence_media_asset_ids": [media_id],
    }


async def _report_issue(
    client: AsyncClient,
    created: dict[str, Any],
    base_id: str,
    media_id: str,
    *,
    payload: dict[str, Any] | None = None,
    role: str = "field_worker",
) -> tuple[dict[str, Any], dict[str, Any]]:
    command = payload or _issue_payload(created, base_id, media_id)
    response = await client.post(
        f"/api/v1/move-jobs/{created['job']['id']}/field-issues",
        headers=_headers(created, role),
        json=command,
    )
    assert response.status_code == 201
    return cast(dict[str, Any], response.json()), command


def _proposal_payload(
    created: dict[str, Any],
    issue_id: str,
    base_id: str,
    *,
    base_amount: int = 1_280_000,
    total_amount: int = 1_430_000,
) -> dict[str, Any]:
    return {
        "field_issue_id": issue_id,
        "base_scope_version_id": base_id,
        "title": "사다리차 1대 추가",
        "reason": "엘리베이터 고장으로 사다리차 하차가 필요합니다",
        "proposed_content": _content(created, "소파 포장·사다리차 하차와 운반"),
        "quote": {
            "base_amount_krw": base_amount,
            "adjustments": [{"label": "사다리차", "amount_krw": total_amount - base_amount}],
            "total_amount_krw": total_amount,
        },
    }


async def _create_proposal(
    client: AsyncClient,
    created: dict[str, Any],
    issue_id: str,
    base_id: str,
    *,
    payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    command = payload or _proposal_payload(created, issue_id, base_id)
    response = await client.post(
        f"/api/v1/move-jobs/{created['job']['id']}/change-proposals",
        headers=_headers(created, "company_manager"),
        json=command,
    )
    assert response.status_code == 201
    return cast(dict[str, Any], response.json()), command


@pytest.mark.anyio
async def test_field_issue_change_proposal_clarification_and_approval(
    field_change_api: FieldChangeApi,
) -> None:
    client, factory, _ = field_change_api
    created = await _create_job(client)
    base = await _confirmed_scope(client, created)
    base_id = base["result_scope_version_id"]
    media_id = await _ready_evidence(client, factory, created)
    issue, issue_command = await _report_issue(client, created, base_id, media_id)
    job_id = created["job"]["id"]
    issues_url = f"/api/v1/move-jobs/{job_id}/field-issues"

    assert issue["status"] == "open"
    assert issue["reported_by_role"] == "field_worker"
    assert issue["evidence_media_asset_ids"] == [media_id]
    replayed_issue = await client.post(
        issues_url,
        headers=_headers(created, "field_worker"),
        json=issue_command,
    )
    assert replayed_issue.status_code == 201
    assert replayed_issue.json()["field_issue_id"] == issue["field_issue_id"]
    conflicting_issue = await client.post(
        issues_url,
        headers=_headers(created, "field_worker"),
        json={**issue_command, "title": "다른 제목"},
    )
    assert conflicting_issue.status_code == 409

    proposal, proposal_command = await _create_proposal(
        client,
        created,
        issue["field_issue_id"],
        base_id,
    )
    proposal_id = proposal["proposal_id"]
    proposal_url = f"/api/v1/move-jobs/{job_id}/change-proposals/{proposal_id}"
    assert proposal["status"] == "pending"
    assert proposal["quote"]["total_amount_krw"] == 1_430_000
    replayed_proposal = await client.post(
        f"/api/v1/move-jobs/{job_id}/change-proposals",
        headers=_headers(created, "company_manager"),
        json=proposal_command,
    )
    assert replayed_proposal.status_code == 201
    assert replayed_proposal.json()["proposal_id"] == proposal_id
    conflict = await client.post(
        f"/api/v1/move-jobs/{job_id}/change-proposals",
        headers=_headers(created, "company_manager"),
        json={**proposal_command, "reason": "다른 제안"},
    )
    assert conflict.status_code == 409

    for role in ("customer", "company_manager"):
        view = await client.get(proposal_url, headers=_headers(created, role))
        assert view.status_code == 200
        assert view.headers["cache-control"] == "no-store"
        assert view.json()["job"]["viewer_role"] == role
        assert view.json()["base_scope_version_label"] == "v2"
        assert view.json()["evidence_media"][0]["media_asset_id"] == media_id
        assert view.json()["evidence_media"][0]["read_url"].startswith("https://")
        assert "object_key" not in view.text
        assert "generation" not in view.json()["evidence_media"][0]

    listed = await client.get(issues_url, headers=_headers(created, "company_manager"))
    assert listed.status_code == 200
    assert listed.json()[0]["status"] == "customer_review"
    assert listed.json()[0]["change_proposal_id"] == proposal_id
    customer_issues = await client.get(issues_url, headers=_headers(created, "customer"))
    assert customer_issues.status_code == 200
    assert customer_issues.json()[0]["change_proposal_id"] == proposal_id

    decision_url = f"{proposal_url}/decision"
    clarification_payload = {
        "decision": "request_clarification",
        "note": "사다리차 진입 위치를 설명해 주세요",
    }
    clarification = await client.post(
        decision_url,
        headers=_headers(created, "customer"),
        json=clarification_payload,
    )
    assert clarification.status_code == 200
    assert clarification.json()["status"] == "clarification_requested"
    clarified_issue = await client.get(
        issues_url,
        headers=_headers(created, "company_manager"),
    )
    assert clarified_issue.json()[0]["status"] == "clarification_requested"
    clarification_replay = await client.post(
        decision_url,
        headers=_headers(created, "customer"),
        json=clarification_payload,
    )
    assert clarification_replay.status_code == 200
    assert (
        clarification_replay.json()["clarification_requested_at"]
        == clarification.json()["clarification_requested_at"]
    )
    assert (
        await client.post(
            decision_url,
            headers=_headers(created, "customer"),
            json={"decision": "reject", "note": "아직 거절"},
        )
    ).status_code == 409

    explanation_url = f"{proposal_url}/explanation"
    explanation_payload = {"explanation": "관리실 확인 후 창측 진입 동선을 확보했습니다"}
    explained = await client.post(
        explanation_url,
        headers=_headers(created, "company_manager"),
        json=explanation_payload,
    )
    assert explained.status_code == 200
    assert explained.json()["status"] == "pending"
    explanation_replay = await client.post(
        explanation_url,
        headers=_headers(created, "company_manager"),
        json=explanation_payload,
    )
    assert explanation_replay.status_code == 200
    assert (
        await client.post(
            explanation_url,
            headers=_headers(created, "company_manager"),
            json={"explanation": "다른 설명"},
        )
    ).status_code == 409

    approve_payload = {"decision": "approve", "note": "증빙과 동선 확인"}
    async with factory.begin() as session:
        asset = await session.get(MediaAsset, UUID(media_id))
        assert asset is not None
        asset.status = MediaAssetStatus.UPLOADED
    assert (
        await client.post(
            decision_url,
            headers=_headers(created, "customer"),
            json=approve_payload,
        )
    ).status_code == 409
    async with factory.begin() as session:
        asset = await session.get(MediaAsset, UUID(media_id))
        assert asset is not None
        asset.status = MediaAssetStatus.READY
    approved = await client.post(
        decision_url,
        headers=_headers(created, "customer"),
        json=approve_payload,
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    result_id = approved.json()["result_scope_version_id"]
    assert result_id is not None
    approval_replay = await client.post(
        decision_url,
        headers=_headers(created, "customer"),
        json=approve_payload,
    )
    assert approval_replay.status_code == 200
    assert approval_replay.json()["decided_at"] == approved.json()["decided_at"]
    approved_issue = await client.get(
        issues_url,
        headers=_headers(created, "company_manager"),
    )
    assert approved_issue.json()[0]["status"] == "approved"
    assert (
        await client.post(
            decision_url,
            headers=_headers(created, "company_manager"),
            json={"decision": "approve"},
        )
    ).status_code == 403

    final_view = await client.get(proposal_url, headers=_headers(created, "customer"))
    assert final_view.status_code == 200
    assert final_view.json()["status"] == "approved"
    assert final_view.json()["decided_by"]["role"] == "customer"
    assert final_view.json()["explanation"] == explanation_payload["explanation"]
    agreement = await client.get(
        f"/api/v1/move-jobs/{job_id}/scope-review",
        headers=_headers(created, "customer"),
    )
    assert agreement.status_code == 200
    agreement_body = agreement.json()
    assert agreement_body["scope"]["id"] == result_id
    assert agreement_body["scope"]["status"] == "confirmed"
    assert agreement_body["collaboration_status"] == "confirmed"
    assert agreement_body["quote"]["total_amount_krw"] == 1_430_000
    assert agreement_body["scope"]["included_works"] == ["포장", "운반"]
    assert agreement_body["scope"]["exclusions"] == ["에어컨 이전"]
    assert agreement_body["customer_confirmed_at"] is not None
    assert agreement_body["company_confirmed_at"] is not None
    assert agreement_body["approved_changes"] == [
        {
            "proposal_id": proposal_id,
            "field_issue_id": issue["field_issue_id"],
            "title": proposal_command["title"],
            "reason": proposal_command["reason"],
            "base_scope_version_id": base_id,
            "result_scope_version_id": result_id,
            "quote": proposal_command["quote"],
            "evidence_media_asset_ids": [media_id],
            "approved_at": approved.json()["decided_at"],
        }
    ]
    async with factory() as session:
        result = await session.get(ScopeVersion, UUID(result_id))
        assert result is not None
        assert result.locked_at is not None
        assert result.parent_version_id == UUID(base_id)
        roles = set(
            (
                await session.scalars(
                    select(ScopeApproval.role).where(ScopeApproval.scope_version_id == result.id)
                )
            ).all()
        )
        assert {role.value for role in roles} == {"customer", "company_manager"}

    second_media_id = await _ready_evidence(client, factory, created)
    second_issue, _ = await _report_issue(client, created, result_id, second_media_id)
    second_payload = _proposal_payload(
        created,
        second_issue["field_issue_id"],
        result_id,
        base_amount=1_430_000,
        total_amount=1_480_000,
    )
    second_payload["proposed_content"] = _content(created, "소파 보강 포장과 사다리차 운반")
    second, _ = await _create_proposal(
        client,
        created,
        second_issue["field_issue_id"],
        result_id,
        payload=second_payload,
    )
    assert second["quote"]["base_amount_krw"] == 1_430_000


@pytest.mark.anyio
async def test_change_proposal_rejection_is_replay_safe_and_keeps_scope(
    field_change_api: FieldChangeApi,
) -> None:
    client, factory, _ = field_change_api
    created = await _create_job(client, "거절 테스트")
    base = await _confirmed_scope(client, created)
    base_id = base["result_scope_version_id"]
    media_id = await _ready_evidence(client, factory, created)
    issue, _ = await _report_issue(client, created, base_id, media_id)
    proposal, _ = await _create_proposal(
        client,
        created,
        issue["field_issue_id"],
        base_id,
    )
    decision_url = (
        f"/api/v1/move-jobs/{created['job']['id']}/change-proposals/"
        f"{proposal['proposal_id']}/decision"
    )
    payload = {"decision": "reject", "note": "외부 협의 후 진행하지 않음"}
    rejected = await client.post(
        decision_url,
        headers=_headers(created, "customer"),
        json=payload,
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["result_scope_version_id"] is None
    replay = await client.post(
        decision_url,
        headers=_headers(created, "customer"),
        json=payload,
    )
    assert replay.status_code == 200
    assert replay.json()["decided_at"] == rejected.json()["decided_at"]
    issues = await client.get(
        f"/api/v1/move-jobs/{created['job']['id']}/field-issues",
        headers=_headers(created, "company_manager"),
    )
    assert issues.json()[0]["status"] == "rejected"
    assert (
        await client.post(
            decision_url,
            headers=_headers(created, "customer"),
            json={"decision": "approve"},
        )
    ).status_code == 409
    async with factory() as session:
        versions = (
            await session.scalars(
                select(ScopeVersion).where(ScopeVersion.job_id == UUID(created["job"]["id"]))
            )
        ).all()
        assert len(versions) == 2


@pytest.mark.anyio
async def test_field_change_permissions_stale_inputs_and_provider_failures(
    field_change_api: FieldChangeApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory, storage = field_change_api
    created = await _create_job(client, "방어 테스트")
    other = await _create_job(client, "다른 작업")
    base = await _confirmed_scope(client, created)
    base_id = base["result_scope_version_id"]
    media_id = await _ready_evidence(client, factory, created, ready=False)
    issue_payload = _issue_payload(created, base_id, media_id)
    issues_url = f"/api/v1/move-jobs/{created['job']['id']}/field-issues"

    company_media_id = await _ready_evidence(
        client,
        factory,
        created,
        ready=False,
        role="company_manager",
    )
    company_issue, company_command = await _report_issue(
        client,
        created,
        base_id,
        company_media_id,
        payload=_issue_payload(created, base_id, company_media_id),
        role="company_manager",
    )
    assert company_issue["reported_by_role"] == "company_manager"
    company_replay = await client.post(
        issues_url,
        headers=_headers(created, "company_manager"),
        json=company_command,
    )
    assert company_replay.status_code == 201
    assert company_replay.json()["field_issue_id"] == company_issue["field_issue_id"]
    assert (
        await client.post(
            issues_url,
            headers=_headers(created, "customer"),
            json={
                **issue_payload,
                "client_reference": str(uuid4()),
            },
        )
    ).status_code == 403
    cross_actor_evidence = await client.post(
        issues_url,
        headers=_headers(created, "company_manager"),
        json={
            **issue_payload,
            "client_reference": str(uuid4()),
        },
    )
    assert cross_actor_evidence.status_code == 409
    assert (
        await client.post(
            issues_url,
            headers=_headers(other, "field_worker"),
            json=issue_payload,
        )
    ).status_code == 404
    wrong_evidence = await client.post(
        issues_url,
        headers=_headers(created, "field_worker"),
        json={**issue_payload, "evidence_media_asset_ids": [str(uuid4())]},
    )
    assert wrong_evidence.status_code == 409
    missing_scope = await client.post(
        issues_url,
        headers=_headers(created, "field_worker"),
        json={
            **issue_payload,
            "client_reference": str(uuid4()),
            "base_scope_version_id": str(uuid4()),
        },
    )
    assert missing_scope.status_code == 404
    issue, _ = await _report_issue(
        client,
        created,
        base_id,
        media_id,
        payload=issue_payload,
    )
    proposal_url = f"/api/v1/move-jobs/{created['job']['id']}/change-proposals"
    proposal_payload = _proposal_payload(created, issue["field_issue_id"], base_id)
    assert (
        await client.post(
            proposal_url,
            headers=_headers(created, "customer"),
            json=proposal_payload,
        )
    ).status_code == 403
    async with factory.begin() as session:
        asset = await session.get(MediaAsset, UUID(media_id))
        assert asset is not None
        asset.status = MediaAssetStatus.PROCESSING
    not_ready = await client.post(
        proposal_url,
        headers=_headers(created, "company_manager"),
        json=proposal_payload,
    )
    assert not_ready.status_code == 409

    mismatched_issue_base = await client.post(
        proposal_url,
        headers=_headers(created, "company_manager"),
        json={**proposal_payload, "base_scope_version_id": str(uuid4())},
    )
    assert mismatched_issue_base.status_code == 409

    async with factory.begin() as session:
        asset = await session.get(MediaAsset, UUID(media_id))
        assert asset is not None
        asset.status = MediaAssetStatus.UPLOADED
    wrong_base_amount = await client.post(
        proposal_url,
        headers=_headers(created, "company_manager"),
        json=_proposal_payload(
            created,
            issue["field_issue_id"],
            base_id,
            base_amount=1_000_000,
            total_amount=1_150_000,
        ),
    )
    assert wrong_base_amount.status_code == 409
    same_content = await client.post(
        proposal_url,
        headers=_headers(created, "company_manager"),
        json={
            **proposal_payload,
            "proposed_content": _content(created, "소파 일반 포장과 운반"),
        },
    )
    assert same_content.status_code == 409
    invalid_zone_payload = {
        **proposal_payload,
        "proposed_content": {
            "schema_version": 1,
            "items": [
                {
                    "item_key": "sofa",
                    "room_zone_id": str(uuid4()),
                    "description": "다른 공간",
                }
            ],
        },
    }
    assert (
        await client.post(
            proposal_url,
            headers=_headers(created, "company_manager"),
            json=invalid_zone_payload,
        )
    ).status_code == 404

    def fail_event_enqueue(*_: object, **__: object) -> None:
        raise IntegrityError("insert", {}, Exception("synthetic race"))

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            field_change_service,
            "enqueue_domain_event",
            fail_event_enqueue,
        )
        raced = await client.post(
            proposal_url,
            headers=_headers(created, "company_manager"),
            json=proposal_payload,
        )
    assert raced.status_code == 409

    proposal, _ = await _create_proposal(
        client,
        created,
        issue["field_issue_id"],
        base_id,
        payload=proposal_payload,
    )
    detail_url = f"{proposal_url}/{proposal['proposal_id']}"
    assert (
        await client.get(detail_url, headers=_headers(created, "field_worker"))
    ).status_code == 403
    assert (
        await client.get(
            detail_url.replace(created["job"]["id"], other["job"]["id"]),
            headers=_headers(other, "customer"),
        )
    ).status_code == 404
    random_decision_url = f"{proposal_url}/{uuid4()}/decision"
    assert (
        await client.post(
            random_decision_url,
            headers=_headers(created, "customer"),
            json={"decision": "approve"},
        )
    ).status_code == 404
    random_explanation_url = f"{proposal_url}/{uuid4()}/explanation"
    assert (
        await client.post(
            random_explanation_url,
            headers=_headers(created, "company_manager"),
            json={"explanation": "없는 제안"},
        )
    ).status_code == 404

    async with factory.begin() as session:
        asset = await session.get(MediaAsset, UUID(media_id))
        assert asset is not None
        asset.status = MediaAssetStatus.UPLOADED
    assert (await client.get(detail_url, headers=_headers(created, "customer"))).status_code == 200
    async with factory.begin() as session:
        asset = await session.get(MediaAsset, UUID(media_id))
        assert asset is not None
        asset.status = MediaAssetStatus.PROCESSING
    assert (await client.get(detail_url, headers=_headers(created, "customer"))).status_code == 409
    async with factory.begin() as session:
        asset = await session.get(MediaAsset, UUID(media_id))
        assert asset is not None
        asset.status = MediaAssetStatus.UPLOADED
        asset.generation = " 7"
    assert (await client.get(detail_url, headers=_headers(created, "customer"))).status_code == 409
    async with factory.begin() as session:
        asset = await session.get(MediaAsset, UUID(media_id))
        assert asset is not None
        asset.generation = "7"

    async def unavailable_read_url(**_: object) -> str:
        raise ProviderError(
            ProviderErrorKind.UNAVAILABLE,
            "synthetic provider failure",
            retryable=True,
        )

    monkeypatch.setattr(storage, "create_read_url", unavailable_read_url)
    assert (await client.get(detail_url, headers=_headers(created, "customer"))).status_code == 503
    monkeypatch.setattr(
        storage,
        "create_read_url",
        AsyncMock(return_value="http://storage.invalid/read/object"),
    )
    assert (await client.get(detail_url, headers=_headers(created, "customer"))).status_code == 503


def test_field_change_command_contracts_reject_invalid_payloads() -> None:
    zone_id = uuid4()
    media_id = uuid4()
    with pytest.raises(ValidationError, match="evidence IDs must be unique"):
        FieldIssueCreate.model_validate(
            {
                "client_reference": str(uuid4()),
                "base_scope_version_id": str(uuid4()),
                "issue_type": "damage_risk",
                "title": "파손 위험",
                "description": "보강 필요",
                "evidence_media_asset_ids": [str(media_id), str(media_id)],
            }
        )
    with pytest.raises(ValidationError, match="require a note"):
        ChangeProposalDecisionCreate.model_validate({"decision": "reject"})
    with pytest.raises(ValidationError, match="require a note"):
        ChangeProposalDecisionCreate.model_validate({"decision": "request_clarification"})
    valid = ChangeProposalCreate.model_validate(
        {
            "field_issue_id": str(uuid4()),
            "base_scope_version_id": str(uuid4()),
            "title": "추가 작업",
            "reason": "근거",
            "proposed_content": {
                "items": [
                    {
                        "item_key": "sofa",
                        "room_zone_id": str(zone_id),
                        "description": "보강 운반",
                    }
                ]
            },
            "quote": {
                "base_amount_krw": 100,
                "adjustments": [],
                "total_amount_krw": 100,
            },
        }
    )
    assert valid.quote.total_amount_krw == 100


@pytest.mark.anyio
async def test_field_change_service_maps_integrity_and_missing_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock(spec=AsyncSession)
    session.scalar = AsyncMock(return_value=None)
    with pytest.raises(FieldChangeNotFoundError):
        await field_change_service._require_participant(
            session,
            uuid4(),
            uuid4(),
            ParticipantRole.FIELD_WORKER,
        )
    with pytest.raises(FieldChangeNotFoundError):
        await field_change_service._require_open_job(session, uuid4())
    with pytest.raises(FieldChangeNotFoundError):
        await field_change_service._require_current_locked_scope(
            session,
            uuid4(),
            uuid4(),
        )
    execute_result = MagicMock()
    execute_result.tuples.return_value.one_or_none.return_value = None
    session.execute = AsyncMock(return_value=execute_result)
    with pytest.raises(FieldChangeNotFoundError):
        await field_change_service._load_proposal(session, uuid4(), uuid4())

    issue = FieldIssue(
        job_id=uuid4(),
        client_reference=uuid4(),
        base_scope_version_id=uuid4(),
        reported_by_participant_id=uuid4(),
        issue_type=FieldIssueType.OUT_OF_SCOPE,
        title="합성",
        description="합성",
        created_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        field_change_service,
        "_require_open_job",
        AsyncMock(),
    )
    monkeypatch.setattr(
        field_change_service,
        "_require_participant",
        AsyncMock(),
    )
    monkeypatch.setattr(
        field_change_service,
        "_require_current_locked_scope",
        AsyncMock(),
    )
    command = FieldIssueCreate(
        client_reference=issue.client_reference,
        base_scope_version_id=issue.base_scope_version_id,
        issue_type=issue.issue_type,
        title=issue.title,
        description=issue.description,
        evidence_media_asset_ids=(uuid4(),),
    )
    session.scalar = AsyncMock(side_effect=[None, None])
    with pytest.raises(FieldChangeNotFoundError):
        await create_field_issue(
            session,
            issue.job_id,
            issue.reported_by_participant_id,
            command,
        )
    session.scalar = AsyncMock(
        side_effect=[None, SimpleNamespace(role=ParticipantRole.FIELD_WORKER)]
    )
    scalars = MagicMock()
    scalars.all.return_value = [
        cast(Any, type("Asset", (), {"id": command.evidence_media_asset_ids[0]})())
    ]
    session.scalars = AsyncMock(return_value=scalars)
    session.flush = AsyncMock(side_effect=IntegrityError("insert", {}, Exception("boom")))
    with pytest.raises(FieldChangeConflictError):
        await create_field_issue(
            session,
            issue.job_id,
            issue.reported_by_participant_id,
            command,
        )

    proposal_command = ChangeProposalCreate.model_validate(
        {
            "field_issue_id": str(uuid4()),
            "base_scope_version_id": str(uuid4()),
            "title": "합성 변경",
            "reason": "합성 근거",
            "proposed_content": {
                "items": [
                    {
                        "item_key": "x",
                        "room_zone_id": str(uuid4()),
                        "description": "y",
                    }
                ]
            },
            "quote": {
                "base_amount_krw": 1,
                "adjustments": [],
                "total_amount_krw": 1,
            },
        }
    )
    session.scalar = AsyncMock(return_value=None)
    with pytest.raises(FieldChangeNotFoundError):
        await create_change_proposal(
            session,
            uuid4(),
            uuid4(),
            proposal_command,
            trace_id=str(uuid4()),
        )


@pytest.mark.anyio
async def test_field_change_defensive_state_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock(spec=AsyncSession)
    completed_job = SimpleNamespace(status=MoveJobStatus.COMPLETED)
    session.scalar = AsyncMock(return_value=completed_job)
    with pytest.raises(FieldChangeConflictError):
        await field_change_service._require_open_job(session, uuid4())

    active_job = SimpleNamespace(status=MoveJobStatus.ACTIVE)
    session.scalar = AsyncMock(side_effect=[active_job, uuid4()])
    with pytest.raises(FieldChangeConflictError):
        await field_change_service._require_open_job(session, uuid4())

    unlocked = SimpleNamespace(locked_at=None)
    session.scalar = AsyncMock(return_value=unlocked)
    with pytest.raises(FieldChangeConflictError):
        await field_change_service._require_current_locked_scope(
            session,
            uuid4(),
            uuid4(),
        )
    locked = SimpleNamespace(locked_at=datetime.now(UTC))
    session.scalar = AsyncMock(side_effect=[locked, uuid4()])
    with pytest.raises(FieldChangeConflictError):
        await field_change_service._require_current_locked_scope(
            session,
            uuid4(),
            uuid4(),
        )

    session.scalar = AsyncMock(side_effect=[None, None])
    with pytest.raises(FieldChangeConflictError):
        await field_change_service._effective_total_amount(session, uuid4(), uuid4())

    now = datetime.now(UTC)
    job_id = uuid4()
    issue = FieldIssue(
        id=uuid4(),
        job_id=job_id,
        client_reference=uuid4(),
        base_scope_version_id=uuid4(),
        reported_by_participant_id=uuid4(),
        issue_type=FieldIssueType.DAMAGE_RISK,
        title="파손 위험",
        description="합성 설명",
        created_at=now,
    )
    request = ChangeRequest(
        id=uuid4(),
        job_id=job_id,
        base_scope_version_id=issue.base_scope_version_id,
        requested_by_participant_id=uuid4(),
        description="합성 변경",
        proposed_content={
            "schema_version": 1,
            "items": [
                {
                    "item_key": "x",
                    "room_zone_id": str(uuid4()),
                    "description": "y",
                }
            ],
        },
        status=ChangeRequestStatus.PENDING,
        created_at=now,
    )
    detail = ChangeProposalDetail(
        change_request_id=request.id,
        field_issue_id=issue.id,
        title="합성 변경",
        base_amount_krw=1,
        adjustments=[],
        total_amount_krw=1,
        created_at=now,
    )
    monkeypatch.setattr(
        field_change_service,
        "_issue_evidence_ids",
        AsyncMock(return_value=()),
    )
    session.scalar = AsyncMock(return_value=ParticipantRole.FIELD_WORKER)
    response = await field_change_service._field_issue_response(
        session,
        issue,
        (detail, request),
    )
    assert response.status == "customer_review"

    monkeypatch.setattr(
        field_change_service,
        "_load_proposal",
        AsyncMock(return_value=(detail, request, issue)),
    )
    storage = FakeObjectStorage()
    session.scalar = AsyncMock(return_value=None)
    with pytest.raises(FieldChangeNotFoundError):
        await field_change_service.get_change_proposal(
            session,
            storage,
            job_id,
            request.id,
            uuid4(),
            ParticipantRole.CUSTOMER,
        )

    viewer_id = uuid4()
    viewer = JobParticipant(
        id=viewer_id,
        job_id=job_id,
        role=ParticipantRole.CUSTOMER,
        display_name="합성 고객",
        created_at=now,
    )
    job = MoveJob(
        id=job_id,
        title="합성 작업",
        status=MoveJobStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    job.participants = []
    job.locations = []
    session.scalar = AsyncMock(return_value=job)
    with pytest.raises(FieldChangeNotFoundError):
        await field_change_service.get_change_proposal(
            session,
            storage,
            job_id,
            request.id,
            viewer_id,
            ParticipantRole.CUSTOMER,
        )

    job.participants = [viewer]
    session.scalar = AsyncMock(return_value=job)
    session.get = AsyncMock(return_value=None)
    with pytest.raises(FieldChangeConflictError):
        await field_change_service.get_change_proposal(
            session,
            storage,
            job_id,
            request.id,
            viewer_id,
            ParticipantRole.CUSTOMER,
        )

    base = ScopeVersion(
        id=request.base_scope_version_id,
        job_id=job_id,
        sequence_number=1,
        content=request.proposed_content,
        content_hash="a" * 64,
        created_at=now,
    )
    session.scalar = AsyncMock(return_value=job)
    session.get = AsyncMock(return_value=base)
    with pytest.raises(FieldChangeConflictError):
        await field_change_service.get_change_proposal(
            session,
            storage,
            job_id,
            request.id,
            viewer_id,
            ParticipantRole.CUSTOMER,
        )

    monkeypatch.setattr(field_change_service, "_require_participant", AsyncMock())
    monkeypatch.setattr(
        field_change_service,
        "decide_change_request",
        AsyncMock(side_effect=ScopeResourceNotFoundError(request.id)),
    )
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: []))
    approve_command = ChangeProposalDecisionCreate(decision="approve")
    with pytest.raises(FieldChangeConflictError):
        await field_change_service.decide_change_proposal(
            session,
            job_id,
            request.id,
            viewer_id,
            approve_command,
            trace_id=str(uuid4()),
        )

    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: [MediaAssetStatus.READY]))
    with pytest.raises(FieldChangeConflictError):
        await field_change_service.decide_change_proposal(
            session,
            job_id,
            request.id,
            viewer_id,
            approve_command,
            trace_id=str(uuid4()),
        )

    request.status = ChangeRequestStatus.PENDING
    monkeypatch.setattr(
        field_change_service,
        "decide_change_request",
        AsyncMock(
            return_value=SimpleNamespace(
                status=ChangeRequestStatus.APPROVED,
                result_scope_version_id=None,
                decision_note=None,
                decided_by_participant_id=viewer_id,
                decided_at=now,
            )
        ),
    )
    with pytest.raises(FieldChangeConflictError):
        await field_change_service.decide_change_proposal(
            session,
            job_id,
            request.id,
            viewer_id,
            approve_command,
            trace_id=str(uuid4()),
        )
