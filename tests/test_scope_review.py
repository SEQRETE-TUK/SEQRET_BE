"""INT-02 scope review, quote, revision, and confirmation tests."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
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
from app.contracts.ai import AnalysisResult, DraftItem
from app.contracts.fakes import FakeObjectStorage
from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.ports import ProviderError
from app.contracts.primitives import (
    AnalysisRunId,
    CaptureSessionId,
    MediaAssetId,
)
from app.main import create_app
from app.modules.access.models import InvitationStatus
from app.modules.capture.models import CaptureSession, MediaAsset
from app.modules.move_job.models import JobParticipant, MoveJob
from app.modules.scope.models import ScopeApproval, ScopeVersion
from app.modules.scope_review import service as scope_review_service
from app.modules.scope_review.models import (
    ScopeProposal,
    ScopeProposalStatus,
    ScopeRevisionRequest,
)
from app.modules.scope_review.schemas import (
    QuoteAdjustment,
    QuoteSnapshot,
    ScopeProposalCreate,
    ScopeRevisionRequestCreate,
)
from app.modules.scope_review.service import (
    ScopeReviewConflictError,
    ScopeReviewNotFoundError,
    _company_participation_status,
    _validated_read_url,
    create_scope_proposal,
    get_scope_review,
    request_scope_revision,
)
from app.platform.db import Base, create_session_factory


@pytest.fixture
async def scope_review_api(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncClient, async_sessionmaker[AsyncSession], FakeObjectStorage]]:
    database_path = (tmp_path / "scope-review.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        poolclass=NullPool,
    )
    factory = create_session_factory(engine)
    storage = FakeObjectStorage()
    application = create_app(Settings(environment=AppEnvironment.TEST))
    application.state.database_session_factory = factory
    application.state.storage_port = storage
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        yield client, factory, storage
    await engine.dispose()


async def _create_job(client: AsyncClient, title: str = "견적 테스트") -> dict[str, Any]:
    response = await client.post(
        "/api/v1/move-jobs",
        json={
            "title": title,
            "scheduled_at": "2026-09-12T08:00:00+09:00",
            "participants": [
                {"role": "customer", "display_name": "박민서"},
                {"role": "company_manager", "display_name": "한빛이사"},
                {"role": "field_worker", "display_name": "김도윤"},
            ],
            "locations": [
                {
                    "kind": "origin",
                    "label": "마포 성산동",
                    "room_zones": [
                        {"name": "거실", "sort_order": 0},
                        {"name": "침실", "sort_order": 1},
                    ],
                },
                {
                    "kind": "destination",
                    "label": "성동 행당동",
                    "room_zones": [{"name": "도착지", "sort_order": 0}],
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


def _headers(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _content(created: dict[str, Any], *, revision: bool = False) -> dict[str, Any]:
    zones = created["job"]["locations"][0]["room_zones"]
    return {
        "schema_version": 1,
        "items": [
            {
                "item_key": "piano",
                "room_zone_id": zones[0]["id"],
                "description": "업라이트 피아노 전문 운반",
            },
            {
                "item_key": "sofa",
                "room_zone_id": zones[0]["id"],
                "description": "3인 소파 포장과 운반" if revision else "3인 소파 운반",
            },
        ],
    }


async def _create_customer_scope(
    client: AsyncClient,
    created: dict[str, Any],
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/move-jobs/{created['job']['id']}/scope-versions",
        headers=_headers(_secret(created, "customer")),
        json={"content": _content(created)},
    )
    assert response.status_code == 201
    return cast(dict[str, Any], response.json())


def _proposal_payload(
    created: dict[str, Any],
    source_scope_version_id: str,
    *,
    revision: bool = False,
) -> dict[str, Any]:
    base = 1_160_000
    adjustment = 80_000 if revision else 120_000
    return {
        "source_scope_version_id": source_scope_version_id,
        "content": _content(created, revision=revision),
        "quote": {
            "base_amount_krw": base,
            "adjustments": [
                {
                    "label": "포장 보강" if revision else "피아노 추가 인력",
                    "amount_krw": adjustment,
                }
            ],
            "total_amount_krw": base + adjustment,
        },
        "execution_plan": {
            "vehicle_count": 1,
            "vehicle_description": "1톤 탑차",
            "worker_count": 2 if revision else 3,
            "estimated_duration_minutes": 240,
            "notes": "피아노 안전 장비 포함",
        },
        "included_works": ["포장", "운반", "피아노 전문 운반"],
        "exclusions": ["에어컨 이전"],
        "reason": (
            "고객 요청에 따라 소파 포장 작업을 반영했습니다."
            if revision
            else "피아노 안전 운반을 위해 전문 인력이 필요합니다."
        ),
    }


@pytest.mark.anyio
async def test_scope_review_quote_revision_and_confirmation_flow(
    scope_review_api: tuple[
        AsyncClient,
        async_sessionmaker[AsyncSession],
        FakeObjectStorage,
    ],
) -> None:
    client, factory, _ = scope_review_api
    created = await _create_job(client)
    job_id = created["job"]["id"]
    customer_secret = _secret(created, "customer")
    manager_secret = _secret(created, "company_manager")
    worker_secret = _secret(created, "field_worker")
    source = await _create_customer_scope(client, created)
    review_url = f"/api/v1/move-jobs/{job_id}/scope-review"
    proposals_url = f"/api/v1/move-jobs/{job_id}/scope-proposals"

    draft_view = await client.get(review_url, headers=_headers(manager_secret))
    assert draft_view.status_code == 200
    assert draft_view.headers["cache-control"] == "no-store"
    assert draft_view.json()["job"] == {
        "job_id": job_id,
        "job_code": f"MOVE-{UUID(job_id).hex[:8].upper()}",
        "title": "견적 테스트",
        "scheduled_at": "2026-09-12T08:00:00",
        "customer_display_name": "박민서",
        "company_display_name": "한빛이사",
        "viewer_display_name": "한빛이사",
        "viewer_role": "company_manager",
        "origin_summary": "마포 성산동",
        "destination_summary": "성동 행당동",
    }
    assert draft_view.json()["scope"]["status"] == "company_review"
    assert draft_view.json()["company_participation_status"] == "company_joined"
    assert draft_view.json()["collaboration_status"] == "awaiting_company_proposal"
    assert draft_view.json()["agreement_notice"].startswith("전자계약이 아닌")
    assert draft_view.json()["scope"]["content_hash"] == source["content_hash"]
    assert draft_view.json()["scope"]["locked_at"] is None
    assert draft_view.json()["approved_changes"] == []
    assert draft_view.json()["scope"]["item_count"] == 2
    assert draft_view.json()["quote"] is None
    assert (await client.get(review_url, headers=_headers(worker_secret))).status_code == 403

    payload = _proposal_payload(created, source["id"])
    proposed = await client.post(
        proposals_url,
        headers=_headers(manager_secret),
        json=payload,
    )
    assert proposed.status_code == 201
    assert proposed.headers["cache-control"] == "no-store"
    proposal = proposed.json()
    assert proposal["proposal_kind"] == "initial"
    assert proposal["status"] == "customer_review"
    assert proposal["quote"]["total_amount_krw"] == 1_280_000
    assert proposal["execution_plan"] == payload["execution_plan"]

    replay = await client.post(
        proposals_url,
        headers=_headers(manager_secret),
        json=payload,
    )
    assert replay.status_code == 201
    assert replay.json() == proposal
    conflicting_payload = _proposal_payload(created, source["id"])
    conflicting_payload["reason"] = "다른 제안"
    assert (
        await client.post(
            proposals_url,
            headers=_headers(manager_secret),
            json=conflicting_payload,
        )
    ).status_code == 409
    assert (
        await client.post(
            proposals_url,
            headers=_headers(manager_secret),
            json=_proposal_payload(
                created,
                proposal["result_scope_version_id"],
                revision=True,
            ),
        )
    ).status_code == 409

    customer_view = await client.get(review_url, headers=_headers(customer_secret))
    assert customer_view.status_code == 200
    assert customer_view.json()["scope"]["status"] == "customer_review"
    assert customer_view.json()["collaboration_status"] == "awaiting_confirmation"
    assert customer_view.json()["scope"]["version_label"] == "v2"
    assert customer_view.json()["scope"]["work_count"] == 3
    assert customer_view.json()["scope"]["exclusion_count"] == 1
    assert customer_view.json()["company_confirmed_at"] is not None
    assert customer_view.json()["customer_confirmed_at"] is None
    assert customer_view.json()["execution_plan"] == payload["execution_plan"]
    revision_url = f"{review_url}/revision-request"
    revision_command = {
        "scope_version_id": proposal["result_scope_version_id"],
        "reason": "소파 포장을 추가해 주세요.",
    }
    requested = await client.post(
        revision_url,
        headers=_headers(customer_secret),
        json=revision_command,
    )
    assert requested.status_code == 201
    assert requested.json()["status"] == "requested"
    assert (
        await client.post(
            revision_url,
            headers=_headers(customer_secret),
            json=revision_command,
        )
    ).json() == requested.json()
    different_request = dict(revision_command, reason="다른 수정 요청")
    assert (
        await client.post(
            revision_url,
            headers=_headers(customer_secret),
            json=different_request,
        )
    ).status_code == 409
    assert (
        await client.post(
            revision_url,
            headers=_headers(manager_secret),
            json=revision_command,
        )
    ).status_code == 403

    confirm_url = f"{review_url}/confirm"
    assert (
        await client.post(
            confirm_url,
            headers=_headers(customer_secret),
            json={"scope_version_id": proposal["result_scope_version_id"]},
        )
    ).status_code == 409
    requested_view = await client.get(review_url, headers=_headers(manager_secret))
    assert requested_view.json()["scope"]["status"] == "revision_requested"
    assert requested_view.json()["collaboration_status"] == "revision_requested"
    assert requested_view.json()["revision_request"]["status"] == "requested"

    revision_payload = _proposal_payload(
        created,
        proposal["result_scope_version_id"],
        revision=True,
    )
    revised = await client.post(
        proposals_url,
        headers=_headers(manager_secret),
        json=revision_payload,
    )
    assert revised.status_code == 201
    assert revised.json()["proposal_kind"] == "revision"
    assert revised.json()["result_scope_version_id"] != proposal["result_scope_version_id"]

    old_confirm = await client.post(
        confirm_url,
        headers=_headers(customer_secret),
        json={"scope_version_id": proposal["result_scope_version_id"]},
    )
    assert old_confirm.status_code == 409
    confirmed = await client.post(
        confirm_url,
        headers=_headers(customer_secret),
        json={"scope_version_id": revised.json()["result_scope_version_id"]},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    assert (
        await client.post(
            confirm_url,
            headers=_headers(customer_secret),
            json={"scope_version_id": revised.json()["result_scope_version_id"]},
        )
    ).json() == confirmed.json()

    final_view = await client.get(review_url, headers=_headers(customer_secret))
    assert final_view.json()["scope"]["status"] == "confirmed"
    assert final_view.json()["collaboration_status"] == "confirmed"
    assert final_view.json()["scope"]["version_label"] == "v3"
    assert final_view.json()["scope"]["locked_at"] is not None
    assert final_view.json()["customer_confirmed_at"] == confirmed.json()["confirmed_at"]
    assert final_view.json()["revision_request"] is None
    assert (
        await client.post(
            proposals_url,
            headers=_headers(manager_secret),
            json=_proposal_payload(
                created,
                revised.json()["result_scope_version_id"],
                revision=True,
            ),
        )
    ).status_code == 409
    assert (
        await client.post(
            revision_url,
            headers=_headers(customer_secret),
            json={
                "scope_version_id": revised.json()["result_scope_version_id"],
                "reason": "이미 확정된 견적 수정",
            },
        )
    ).status_code == 409

    async with factory() as session:
        proposals = (
            await session.scalars(
                select(ScopeProposal)
                .where(ScopeProposal.job_id == UUID(job_id))
                .order_by(ScopeProposal.sent_at)
            )
        ).all()
        revision = await session.scalar(
            select(ScopeRevisionRequest).where(ScopeRevisionRequest.job_id == UUID(job_id))
        )
        approvals = (
            await session.scalars(
                select(ScopeApproval).where(
                    ScopeApproval.scope_version_id
                    == UUID(revised.json()["result_scope_version_id"])
                )
            )
        ).all()
    assert [item.status for item in proposals] == [
        ScopeProposalStatus.SUPERSEDED,
        ScopeProposalStatus.CONFIRMED,
    ]
    assert revision is not None
    assert revision.resolved_by_scope_proposal_id == proposals[1].id
    assert revision.resolved_at is not None
    assert {approval.role.value for approval in approvals} == {
        "customer",
        "company_manager",
    }


@pytest.mark.anyio
async def test_scope_review_exposes_company_invitation_participation(
    scope_review_api: tuple[
        AsyncClient,
        async_sessionmaker[AsyncSession],
        FakeObjectStorage,
    ],
) -> None:
    client, _, _ = scope_review_api
    onboarded = await client.post(
        "/api/v1/move-jobs/onboarding",
        json={
            "title": "업체 참여 상태",
            "customer_display_name": "고객",
            "locations": [
                {
                    "kind": "origin",
                    "label": "출발지",
                    "room_zones": [{"name": "거실", "sort_order": 0}],
                }
            ],
        },
    )
    assert onboarded.status_code == 201
    created = onboarded.json()
    job_id = created["job"]["id"]
    customer_secret = created["customer_access_link"]["secret"]
    root = await client.post(
        f"/api/v1/move-jobs/{job_id}/scope-versions",
        headers=_headers(customer_secret),
        json={
            "content": {
                "items": [
                    {
                        "item_key": "box",
                        "room_zone_id": created["job"]["locations"][0]["room_zones"][0]["id"],
                        "description": "박스 운반",
                    }
                ]
            }
        },
    )
    assert root.status_code == 201
    review_url = f"/api/v1/move-jobs/{job_id}/scope-review"
    not_invited = await client.get(review_url, headers=_headers(customer_secret))
    assert not_invited.status_code == 200
    assert not_invited.json()["company_participation_status"] == "company_not_invited"
    assert not_invited.json()["collaboration_status"] == "draft"
    assert not_invited.json()["job"]["company_display_name"] is None

    invited = await client.post(
        f"/api/v1/move-jobs/{job_id}/invitations",
        headers=_headers(customer_secret),
        json={"role": "company_manager", "display_name": "초대 업체"},
    )
    assert invited.status_code == 201
    invitation = invited.json()
    pending = await client.get(review_url, headers=_headers(customer_secret))
    assert pending.json()["company_participation_status"] == "company_invited"
    assert pending.json()["job"]["company_display_name"] is None

    manager_secret = invitation["access_link"]["secret"]
    accepted = await client.post(
        f"/api/v1/move-jobs/{job_id}/invitations/{invitation['invitation']['id']}/accept",
        headers=_headers(manager_secret),
    )
    assert accepted.status_code == 200
    joined = await client.get(review_url, headers=_headers(customer_secret))
    assert joined.json()["company_participation_status"] == "company_joined"
    assert joined.json()["collaboration_status"] == "awaiting_company_proposal"
    assert joined.json()["job"]["company_display_name"] == "초대 업체"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "expires_delta", "expected"),
    [
        (InvitationStatus.PENDING, timedelta(hours=1), "company_invited"),
        (InvitationStatus.PENDING, timedelta(hours=-1), "company_invitation_expired"),
        (InvitationStatus.ACCEPTED, timedelta(hours=1), "company_joined"),
        (InvitationStatus.DECLINED, timedelta(hours=1), "company_declined"),
        (InvitationStatus.EXPIRED, timedelta(hours=-1), "company_invitation_expired"),
        (InvitationStatus.REVOKED, timedelta(hours=1), "company_invitation_revoked"),
    ],
)
async def test_company_participation_maps_invitation_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    status: InvitationStatus,
    expires_delta: timedelta,
    expected: str,
) -> None:
    session = AsyncSession()
    job = MoveJob(id=uuid4(), title="참여 상태")
    job.participants = []
    invitation = SimpleNamespace(
        status=status,
        expires_at=datetime.now(UTC) + expires_delta,
    )
    monkeypatch.setattr(session, "scalar", AsyncMock(return_value=invitation))
    result = await _company_participation_status(session, job)
    assert result.value == expected
    await session.close()


@pytest.mark.anyio
async def test_company_participation_treats_legacy_bootstrap_as_joined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncSession()
    job = MoveJob(id=uuid4(), title="기존 bootstrap")
    job.participants = [
        JobParticipant(
            job_id=job.id,
            role=ParticipantRole.COMPANY_MANAGER,
            display_name="기존 업체",
        )
    ]
    monkeypatch.setattr(session, "scalar", AsyncMock(return_value=None))
    assert (await _company_participation_status(session, job)).value == "company_joined"
    await session.close()


@pytest.mark.anyio
async def test_scope_review_exposes_structured_v2_items(
    scope_review_api: tuple[
        AsyncClient,
        async_sessionmaker[AsyncSession],
        FakeObjectStorage,
    ],
) -> None:
    client, _, _ = scope_review_api
    created = await _create_job(client)
    zone_id = created["job"]["locations"][0]["room_zones"][0]["id"]
    created_scope = await client.post(
        f"/api/v1/move-jobs/{created['job']['id']}/scope-versions",
        headers=_headers(_secret(created, "customer")),
        json={
            "content": {
                "schema_version": 2,
                "items": [
                    {
                        "item_key": "fridge",
                        "room_zone_id": zone_id,
                        "name": "냉장고",
                        "quantity": 1,
                        "unit": "대",
                        "work_note": "문 분리 확인",
                        "review_status": "confirmed",
                        "source": "customer",
                    }
                ],
            }
        },
    )
    assert created_scope.status_code == 201
    response = await client.get(
        f"/api/v1/move-jobs/{created['job']['id']}/scope-review",
        headers=_headers(_secret(created, "company_manager")),
    )
    assert response.status_code == 200
    assert response.json()["scope"]["schema_version"] == 2
    item = response.json()["scope"]["room_groups"][0]["items"][0]
    assert item["description"] == "냉장고"
    assert item["name"] == "냉장고"
    assert item["quantity"] == 1
    assert item["unit"] == "대"
    assert item["work_note"] == "문 분리 확인"
    assert item["review_status"] == "confirmed"
    assert item["source"] == "customer"
    assert (
        response.json()["scope"]["location_conditions"]
        == created_scope.json()["content"]["location_conditions"]
    )


@pytest.mark.anyio
async def test_scope_review_rejects_missing_cross_job_stale_and_wrong_roles(
    scope_review_api: tuple[
        AsyncClient,
        async_sessionmaker[AsyncSession],
        FakeObjectStorage,
    ],
) -> None:
    client, _, _ = scope_review_api
    first = await _create_job(client, "첫 작업")
    second = await _create_job(client, "둘째 작업")
    first_job_id = first["job"]["id"]
    first_manager = _secret(first, "company_manager")
    first_customer = _secret(first, "customer")
    first_worker = _secret(first, "field_worker")
    source = await _create_customer_scope(client, first)
    second_source = await _create_customer_scope(client, second)
    review_url = f"/api/v1/move-jobs/{first_job_id}/scope-review"
    proposals_url = f"/api/v1/move-jobs/{first_job_id}/scope-proposals"

    empty = await _create_job(client, "빈 작업")
    assert (
        await client.get(
            f"/api/v1/move-jobs/{empty['job']['id']}/scope-review",
            headers=_headers(_secret(empty, "customer")),
        )
    ).status_code == 404
    assert (
        await client.get(
            f"/api/v1/move-jobs/{second['job']['id']}/scope-review",
            headers=_headers(first_customer),
        )
    ).status_code == 404

    assert (
        await client.post(
            proposals_url,
            headers=_headers(first_customer),
            json=_proposal_payload(first, source["id"]),
        )
    ).status_code == 403
    assert (
        await client.post(
            proposals_url,
            headers=_headers(first_worker),
            json=_proposal_payload(first, source["id"]),
        )
    ).status_code == 403
    cross_source_payload = _proposal_payload(first, second_source["id"])
    assert (
        await client.post(
            proposals_url,
            headers=_headers(first_manager),
            json=cross_source_payload,
        )
    ).status_code == 404

    child = await client.post(
        f"/api/v1/move-jobs/{first_job_id}/scope-versions",
        headers=_headers(first_customer),
        json={"parent_version_id": source["id"], "content": _content(first, revision=True)},
    )
    assert child.status_code == 201
    assert (
        await client.post(
            proposals_url,
            headers=_headers(first_manager),
            json=_proposal_payload(first, source["id"]),
        )
    ).status_code == 409

    manager_scope = await client.post(
        f"/api/v1/move-jobs/{second['job']['id']}/scope-versions",
        headers=_headers(_secret(second, "company_manager")),
        json={
            "parent_version_id": second_source["id"],
            "content": _content(second, revision=True),
        },
    )
    assert manager_scope.status_code == 201
    assert (
        await client.post(
            f"/api/v1/move-jobs/{second['job']['id']}/scope-proposals",
            headers=_headers(_secret(second, "company_manager")),
            json=_proposal_payload(second, manager_scope.json()["id"]),
        )
    ).status_code == 409

    missing_id = str(uuid4())
    assert (
        await client.post(
            f"{review_url}/revision-request",
            headers=_headers(first_customer),
            json={"scope_version_id": missing_id, "reason": "수정"},
        )
    ).status_code == 404
    assert (
        await client.post(
            f"{review_url}/confirm",
            headers=_headers(first_customer),
            json={"scope_version_id": missing_id},
        )
    ).status_code == 404


@pytest.mark.anyio
async def test_scope_review_rejects_a_revision_when_request_record_is_missing(
    scope_review_api: tuple[
        AsyncClient,
        async_sessionmaker[AsyncSession],
        FakeObjectStorage,
    ],
) -> None:
    client, factory, _ = scope_review_api
    created = await _create_job(client, "수정 기록 무결성")
    job_id = created["job"]["id"]
    source = await _create_customer_scope(client, created)
    proposal = await client.post(
        f"/api/v1/move-jobs/{job_id}/scope-proposals",
        headers=_headers(_secret(created, "company_manager")),
        json=_proposal_payload(created, source["id"]),
    )
    assert proposal.status_code == 201
    revision = await client.post(
        f"/api/v1/move-jobs/{job_id}/scope-review/revision-request",
        headers=_headers(_secret(created, "customer")),
        json={
            "scope_version_id": proposal.json()["result_scope_version_id"],
            "reason": "포장을 보강해 주세요.",
        },
    )
    assert revision.status_code == 201

    async with factory.begin() as session:
        stored = await session.get(
            ScopeRevisionRequest,
            UUID(revision.json()["revision_request_id"]),
        )
        assert stored is not None
        await session.delete(stored)

    replacement = await client.post(
        f"/api/v1/move-jobs/{job_id}/scope-proposals",
        headers=_headers(_secret(created, "company_manager")),
        json=_proposal_payload(
            created,
            proposal.json()["result_scope_version_id"],
            revision=True,
        ),
    )
    assert replacement.status_code == 409


@pytest.mark.anyio
async def test_scope_review_defensive_view_failures(
    scope_review_api: tuple[
        AsyncClient,
        async_sessionmaker[AsyncSession],
        FakeObjectStorage,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory, storage = scope_review_api
    created = await _create_job(client, "방어적 조회")
    job_id = UUID(created["job"]["id"])
    customer_id = _participant_id(created, "customer")
    source = await _create_customer_scope(client, created)

    async with factory() as session:
        with pytest.raises(ScopeReviewNotFoundError):
            await get_scope_review(
                session,
                storage,
                uuid4(),
                customer_id,
                ParticipantRole.CUSTOMER,
            )
        with pytest.raises(ScopeReviewNotFoundError):
            await get_scope_review(
                session,
                storage,
                job_id,
                customer_id,
                ParticipantRole.COMPANY_MANAGER,
            )

    async with factory.begin() as session:
        version = await session.get(ScopeVersion, UUID(source["id"]))
        assert version is not None
        original_content = version.content
        version.content = {
            "schema_version": 1,
            "items": [
                {
                    "item_key": "orphan",
                    "room_zone_id": str(uuid4()),
                    "description": "등록되지 않은 방",
                }
            ],
        }
    conflict = await client.get(
        f"/api/v1/move-jobs/{job_id}/scope-review",
        headers=_headers(_secret(created, "customer")),
    )
    assert conflict.status_code == 409

    async with factory.begin() as session:
        version = await session.get(ScopeVersion, UUID(source["id"]))
        assert version is not None
        version.content = original_content

    monkeypatch.setattr(
        scope_review_service,
        "_proposal_for_result",
        AsyncMock(
            return_value=SimpleNamespace(
                id=uuid4(),
                status="unsupported",
                included_works=[],
                exclusions=[],
            )
        ),
    )
    monkeypatch.setattr(
        scope_review_service,
        "_revision_for_proposal",
        AsyncMock(return_value=None),
    )
    async with factory() as session:
        with pytest.raises(ScopeReviewConflictError):
            await get_scope_review(
                session,
                storage,
                job_id,
                customer_id,
                ParticipantRole.CUSTOMER,
            )


@pytest.mark.anyio
async def test_scope_review_maps_integrity_errors_to_conflicts(
    scope_review_api: tuple[
        AsyncClient,
        async_sessionmaker[AsyncSession],
        FakeObjectStorage,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory, _ = scope_review_api
    created = await _create_job(client, "동시 생성 충돌")
    job_id = UUID(created["job"]["id"])
    source = await _create_customer_scope(client, created)
    manager_id = _participant_id(created, "company_manager")
    customer_id = _participant_id(created, "customer")
    proposal_command = ScopeProposalCreate.model_validate(_proposal_payload(created, source["id"]))

    async with factory() as session:
        original_flush = session.flush
        flush_count = 0

        async def fail_proposal_flush(*args: Any, **kwargs: Any) -> None:
            nonlocal flush_count
            flush_count += 1
            if flush_count == 2:
                raise IntegrityError("duplicate proposal", {}, RuntimeError("duplicate"))
            await original_flush(*args, **kwargs)

        monkeypatch.setattr(session, "flush", fail_proposal_flush)
        with pytest.raises(ScopeReviewConflictError):
            await create_scope_proposal(
                session,
                job_id,
                manager_id,
                proposal_command,
            )
        await session.rollback()

    proposal = await client.post(
        f"/api/v1/move-jobs/{job_id}/scope-proposals",
        headers=_headers(_secret(created, "company_manager")),
        json=_proposal_payload(created, source["id"]),
    )
    assert proposal.status_code == 201
    revision_command = ScopeRevisionRequestCreate(
        scope_version_id=UUID(proposal.json()["result_scope_version_id"]),
        reason="중복 수정 요청",
    )

    async with factory() as session:

        async def fail_revision_flush(*args: Any, **kwargs: Any) -> None:
            raise IntegrityError("duplicate revision", {}, RuntimeError("duplicate"))

        monkeypatch.setattr(session, "flush", fail_revision_flush)
        with pytest.raises(ScopeReviewConflictError):
            await request_scope_revision(
                session,
                job_id,
                customer_id,
                revision_command,
            )
        await session.rollback()


@pytest.mark.parametrize(
    "value",
    (
        " https://storage.invalid/read/object",
        "http://storage.invalid/read/object",
        "https:///read/object",
        "https://storage.invalid:invalid/read/object",
    ),
)
def test_scope_review_rejects_invalid_storage_read_urls(value: str) -> None:
    with pytest.raises(ProviderError, match="invalid read URL"):
        _validated_read_url(value)


def test_scope_review_preserves_legacy_proposal_without_execution_plan() -> None:
    proposal = cast(ScopeProposal, SimpleNamespace(execution_plan=None))

    assert scope_review_service._execution_plan_from_proposal(proposal) is None


def test_scope_proposal_contract_rejects_invalid_money_and_classification() -> None:
    zone_id = uuid4()
    source_id = uuid4()
    base: dict[str, Any] = {
        "source_scope_version_id": str(source_id),
        "content": {
            "items": [
                {
                    "item_key": "sofa",
                    "room_zone_id": str(zone_id),
                    "description": "소파 운반",
                }
            ]
        },
        "quote": {
            "base_amount_krw": 1000,
            "adjustments": [{"label": "추가", "amount_krw": 100}],
            "total_amount_krw": 1100,
        },
        "execution_plan": {
            "vehicle_count": 1,
            "vehicle_description": "1톤 탑차",
            "worker_count": 2,
            "estimated_duration_minutes": 180,
            "notes": None,
        },
        "included_works": ["운반"],
        "exclusions": ["에어컨"],
        "reason": "견적 사유",
    }
    assert ScopeProposalCreate.model_validate(base).quote == QuoteSnapshot(
        base_amount_krw=1000,
        adjustments=(QuoteAdjustment(label="추가", amount_krw=100),),
        total_amount_krw=1100,
    )
    invalid_total = {**base, "quote": {**base["quote"], "total_amount_krw": 1200}}
    with pytest.raises(ValidationError, match="quote total"):
        ScopeProposalCreate.model_validate(invalid_total)
    duplicate_adjustment = {
        **base,
        "quote": {
            "base_amount_krw": 1000,
            "adjustments": [
                {"label": "추가", "amount_krw": 100},
                {"label": "추가", "amount_krw": -100},
            ],
            "total_amount_krw": 1000,
        },
    }
    with pytest.raises(ValidationError, match="labels must be unique"):
        ScopeProposalCreate.model_validate(duplicate_adjustment)
    for plan_field, plan_value in (
        ("vehicle_count", 0),
        ("vehicle_description", ""),
        ("worker_count", 0),
        ("estimated_duration_minutes", 29),
    ):
        invalid_plan = {
            **base,
            "execution_plan": {**base["execution_plan"], plan_field: plan_value},
        }
        with pytest.raises(ValidationError):
            ScopeProposalCreate.model_validate(invalid_plan)
    for field, value, message in (
        ("included_works", ["운반", "운반"], "included works must be unique"),
        ("exclusions", ["에어컨", "에어컨"], "exclusions must be unique"),
        ("exclusions", ["운반"], "must not overlap"),
    ):
        invalid = {**base, field: value}
        with pytest.raises(ValidationError, match=message):
            ScopeProposalCreate.model_validate(invalid)


@pytest.mark.anyio
async def test_scope_review_returns_analysis_media_without_provider_details(
    scope_review_api: tuple[
        AsyncClient,
        async_sessionmaker[AsyncSession],
        FakeObjectStorage,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory, storage = scope_review_api
    created = await _create_job(client, "미디어 근거")
    job_id = UUID(created["job"]["id"])
    customer_id = _participant_id(created, "customer")
    zone_id = UUID(created["job"]["locations"][0]["room_zones"][0]["id"])
    capture_id = uuid4()
    media_id = uuid4()
    source_id = uuid4()
    review_id = uuid4()
    now = datetime.now(UTC)
    result = AnalysisResult(
        analysis_run_id=AnalysisRunId(uuid4()),
        capture_session_id=CaptureSessionId(capture_id),
        model_name="test-model",
        model_version="test-v1",
        prompt_version="prompt-v1",
        draft_items=(
            DraftItem(
                item_key="sofa",
                description="3인 소파",
                confidence=0.72,
                source_media_asset_ids=(MediaAssetId(media_id),),
            ),
        ),
        review_required_items=(
            DraftItem(
                item_key="sofa",
                description="3인 소파",
                confidence=0.72,
                source_media_asset_ids=(MediaAssetId(media_id),),
            ),
        ),
    )
    content = {
        "schema_version": 1,
        "items": [
            {
                "item_key": "sofa",
                "room_zone_id": str(zone_id),
                "description": "3인 소파 포장과 운반",
            }
        ],
    }
    async with factory.begin() as session:
        session.add(
            CaptureSession(
                id=capture_id,
                job_id=job_id,
                created_by_participant_id=customer_id,
                created_at=now,
            )
        )
        session.add(
            MediaAsset(
                id=media_id,
                capture_session_id=capture_id,
                room_zone_id=zone_id,
                media_purpose=MediaPurpose.INVENTORY,
                status=MediaAssetStatus.READY,
                object_key=f"jobs/{job_id}/evidence.jpg",
                content_type="image/jpeg",
                expected_size_bytes=10,
                actual_size_bytes=10,
                sha256_hex="a" * 64,
                generation="7",
                created_at=now,
                uploaded_at=now,
            )
        )
        session.add_all(
            [
                ScopeVersion(
                    id=source_id,
                    job_id=job_id,
                    sequence_number=1,
                    content=content,
                    content_hash="a" * 64,
                    source_analysis_run_id=result.analysis_run_id,
                    source_capture_session_id=capture_id,
                    analysis_source=result.model_dump(mode="json"),
                    created_at=now,
                ),
                ScopeVersion(
                    id=review_id,
                    job_id=job_id,
                    parent_version_id=source_id,
                    sequence_number=2,
                    content=content,
                    content_hash="b" * 64,
                    created_by_participant_id=customer_id,
                    created_at=now,
                ),
            ]
        )

    proposal = await client.post(
        f"/api/v1/move-jobs/{job_id}/scope-proposals",
        headers=_headers(_secret(created, "company_manager")),
        json=_proposal_payload(created, str(review_id)),
    )
    assert proposal.status_code == 201
    response = await client.get(
        f"/api/v1/move-jobs/{job_id}/scope-review",
        headers=_headers(_secret(created, "customer")),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["scope"]["review_required_count"] == 1
    sofa = next(
        item
        for group in body["scope"]["room_groups"]
        for item in group["items"]
        if item["item_key"] == "sofa"
    )
    assert sofa["review_required"] is True
    assert sofa["source_media_asset_ids"] == [str(media_id)]
    assert body["media_previews"][0]["media_asset_id"] == str(media_id)
    assert body["media_previews"][0]["read_url"].startswith("https://storage.invalid/read/")
    serialized = response.text
    assert "test-model" not in serialized
    assert "object_key" not in serialized

    monkeypatch.setattr(
        storage,
        "create_read_url",
        AsyncMock(return_value=" https://storage.invalid/read/evidence"),
    )
    unavailable = await client.get(
        f"/api/v1/move-jobs/{job_id}/scope-review",
        headers=_headers(_secret(created, "customer")),
    )
    assert unavailable.status_code == 503
