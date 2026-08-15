"""INT-04 field completion, customer decision, documents, and retention tests."""

import io
import zipfile
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
from app.contracts.events import DomainEventType
from app.contracts.fakes import FakeObjectStorage
from app.contracts.media import MediaAssetStatus
from app.contracts.ports import ProviderError, ProviderErrorKind
from app.main import create_app
from app.modules.background_job.models import BackgroundJob
from app.modules.capture.models import MediaAsset
from app.modules.completion import documents as completion_documents
from app.modules.completion import router as completion_router
from app.modules.completion import service as completion_service
from app.modules.completion.models import (
    CompletionConfirmation,
    CompletionEvidence,
    CompletionProblemReport,
    CompletionProblemType,
    CompletionRequest,
    CompletionRequestStatus,
    CompletionSubmission,
)
from app.modules.completion.schemas import (
    CompletionDecisionCreate,
    CompletionFieldChangeSummary,
    CompletionRequestCreate,
    CompletionSubmissionCreate,
    CompletionSummaryView,
    CompletionWorkerShiftCreate,
)
from app.modules.dispatch.models import DispatchPlan, DispatchSetup, FieldCheckIn
from app.modules.move_job.models import MoveJobStatus
from app.modules.notification.service import consume_notification_event
from app.modules.scope.models import ScopeVersion
from app.platform.db import Base, create_session_factory
from app.platform.event_bus.models import OutboxEvent
from app.platform.event_bus.service import _to_domain_event

CompletionApi = tuple[
    AsyncClient,
    async_sessionmaker[AsyncSession],
    FakeObjectStorage,
]


@pytest.fixture
async def completion_lifecycle_api(tmp_path: Path) -> AsyncIterator[CompletionApi]:
    database_path = (tmp_path / "completion-lifecycle.sqlite3").as_posix()
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


def _headers(created: dict[str, Any], role: str) -> dict[str, str]:
    secret = next(link["secret"] for link in created["access_links"] if link["role"] == role)
    return {"Authorization": f"Bearer {secret}"}


def _participant_id(created: dict[str, Any], role: str) -> str:
    return cast(
        str,
        next(
            participant["id"]
            for participant in created["job"]["participants"]
            if participant["role"] == role
        ),
    )


def test_completion_contracts_and_pdf_wrapping_reject_ambiguous_input() -> None:
    worker_id = uuid4()
    aware = datetime.now(UTC)
    with pytest.raises(ValidationError, match="timestamps must include a timezone"):
        CompletionWorkerShiftCreate(
            worker_id=worker_id,
            started_at=aware.replace(tzinfo=None),
            ended_at=aware,
        )
    with pytest.raises(ValidationError, match="must end after"):
        CompletionWorkerShiftCreate(
            worker_id=worker_id,
            started_at=aware,
            ended_at=aware - timedelta(minutes=1),
        )

    submission = {
        "client_reference": str(uuid4()),
        "dispatch_id": str(uuid4()),
        "scope_version_id": str(uuid4()),
        "completion_media_asset_ids": [],
        "completed_check_keys": ["done"],
        "worker_shifts": [
            {
                "worker_id": str(worker_id),
                "started_at": aware.isoformat(),
                "ended_at": aware.isoformat(),
            }
        ],
        "onsite_customer_confirmed": True,
        "onsite_confirmed_at": aware.isoformat(),
        "work_ended_at": aware.isoformat(),
    }
    with pytest.raises(ValidationError, match="values must be unique"):
        CompletionSubmissionCreate.model_validate(
            {**submission, "completed_check_keys": ["done", "done"]}
        )
    with pytest.raises(ValidationError, match="timestamps must include a timezone"):
        CompletionSubmissionCreate.model_validate(
            {**submission, "work_ended_at": aware.replace(tzinfo=None).isoformat()}
        )
    with pytest.raises(ValidationError, match="must not contain problem fields"):
        CompletionDecisionCreate(
            decision="confirm",
            problem_type=CompletionProblemType.DAMAGE,
            problem_description="확인과 문제 신고를 동시에 보낼 수 없음",
        )

    assert completion_documents._wrapped(("", "가" * 73)) == ["", "가" * 72, "가"]


async def _create_job(client: AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/move-jobs",
        json={
            "title": "INT-04 완료 lifecycle",
            "scheduled_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "participants": [
                {"role": "customer", "display_name": "완료 고객"},
                {"role": "company_manager", "display_name": "완료 업체"},
                {"role": "field_worker", "display_name": "완료 기사"},
            ],
            "locations": [
                {
                    "kind": "origin",
                    "label": "출발지(마스킹)",
                    "room_zones": [{"name": "거실", "sort_order": 0}],
                },
                {
                    "kind": "destination",
                    "label": "도착지(마스킹)",
                    "room_zones": [{"name": "거실", "sort_order": 0}],
                },
            ],
        },
    )
    assert response.status_code == 201
    return cast(dict[str, Any], response.json())


async def _confirmed_scope(client: AsyncClient, created: dict[str, Any]) -> str:
    job_id = created["job"]["id"]
    zone_id = created["job"]["locations"][0]["room_zones"][0]["id"]
    content = {
        "schema_version": 1,
        "items": [
            {
                "item_key": "sofa",
                "room_zone_id": zone_id,
                "description": "소파 포장 및 운반",
            }
        ],
    }
    root = await client.post(
        f"/api/v1/move-jobs/{job_id}/scope-versions",
        headers=_headers(created, "customer"),
        json={"content": content},
    )
    assert root.status_code == 201
    proposal = await client.post(
        f"/api/v1/move-jobs/{job_id}/scope-proposals",
        headers=_headers(created, "company_manager"),
        json={
            "source_scope_version_id": root.json()["id"],
            "content": content,
            "quote": {
                "base_amount_krw": 550_000,
                "adjustments": [],
                "total_amount_krw": 550_000,
            },
            "included_works": ["포장", "운반"],
            "exclusions": ["가전 설치"],
            "reason": "INT-04 합성 견적",
        },
    )
    assert proposal.status_code == 201
    scope_id = cast(str, proposal.json()["result_scope_version_id"])
    confirmed = await client.post(
        f"/api/v1/move-jobs/{job_id}/scope-review/confirm",
        headers=_headers(created, "customer"),
        json={"scope_version_id": scope_id},
    )
    assert confirmed.status_code == 200
    return scope_id


async def _dispatch_and_check_in(
    client: AsyncClient,
    created: dict[str, Any],
    scope_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    job_id = created["job"]["id"]
    setup_payload = {
        "client_reference": str(uuid4()),
        "source_scope_version_id": scope_id,
        "expected_duration_minutes": 180,
        "required_vehicle_capacity_m2": 20,
        "required_worker_count": 1,
        "required_skills": ["포장"],
        "required_certifications": ["화물운송"],
        "check_in_items": [{"key": "safety", "label": "안전 장비 확인"}],
        "completion_check_items": [
            {"key": "tools_removed", "label": "작업 도구 회수"},
            {"key": "site_restored", "label": "현장 정리"},
        ],
        "origin_conditions": ["엘리베이터 사용 가능"],
        "safety_notice": "보호 장갑 착용",
        "vehicles": [
            {
                "external_reference": "vehicle-ready",
                "display_name": "1톤 윙바디",
                "specification": "적재 25m2",
                "equipment": ["리프트"],
                "capacity_m2": 25,
                "available": True,
            }
        ],
        "workers": [
            {
                "external_reference": "worker-lead",
                "display_name": "완료 기사",
                "role_label": "현장 리더",
                "skills": ["포장"],
                "certifications": ["화물운송"],
                "available": True,
                "participant_id": _participant_id(created, "field_worker"),
            }
        ],
    }
    setup = await client.post(
        f"/api/v1/move-jobs/{job_id}/dispatch/setup",
        headers=_headers(created, "company_manager"),
        json=setup_payload,
    )
    assert setup.status_code == 201
    setup_view = cast(dict[str, Any], setup.json())
    worker_id = setup_view["worker_options"][0]["id"]
    dispatch_payload = {
        "setup_id": setup_view["setup_id"],
        "vehicle_id": setup_view["vehicle_options"][0]["id"],
        "lead_worker_id": worker_id,
        "worker_ids": [worker_id],
        "worker_note": "완료 lifecycle",
    }
    dispatch = await client.put(
        f"/api/v1/move-jobs/{job_id}/dispatch",
        headers=_headers(created, "company_manager"),
        json=dispatch_payload,
    )
    assert dispatch.status_code == 200
    check_in = await client.post(
        f"/api/v1/move-jobs/{job_id}/check-ins",
        headers=_headers(created, "field_worker"),
        json={
            "dispatch_id": dispatch.json()["dispatch_id"],
            "confirmed_check_keys": ["safety"],
        },
    )
    assert check_in.status_code == 201
    return setup_view, cast(dict[str, Any], dispatch.json()), cast(dict[str, Any], check_in.json())


async def _ready_completion_media(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    created: dict[str, Any],
) -> str:
    job_id = created["job"]["id"]
    headers = _headers(created, "field_worker")
    capture = await client.post(
        f"/api/v1/move-jobs/{job_id}/capture-sessions",
        headers=headers,
    )
    assert capture.status_code == 201
    upload = await client.post(
        f"/api/v1/move-jobs/{job_id}/capture-sessions/{capture.json()['id']}/media-assets/upload",
        headers=headers,
        json={
            "room_zone_id": created["job"]["locations"][0]["room_zones"][0]["id"],
            "media_purpose": "completion",
            "content_type": "image/jpeg",
            "content_length": 14,
        },
    )
    assert upload.status_code == 201
    media_id = cast(str, upload.json()["asset"]["id"])
    async with factory.begin() as session:
        asset = await session.get(MediaAsset, UUID(media_id))
        assert asset is not None
        asset.status = MediaAssetStatus.READY
        asset.actual_size_bytes = 14
        asset.sha256_hex = "a" * 64
        asset.generation = "7"
        asset.uploaded_at = datetime.now(UTC)
    return media_id


async def _context(
    api: CompletionApi,
    *,
    with_media: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    client, factory, _ = api
    created = await _create_job(client)
    scope_id = await _confirmed_scope(client, created)
    setup, dispatch, check_in = await _dispatch_and_check_in(client, created, scope_id)
    media_ids = [await _ready_completion_media(client, factory, created)] if with_media else []
    checked_in_at = datetime.fromisoformat(check_in["checked_in_at"])
    completed_at = max(datetime.now(UTC), checked_in_at)
    payload = {
        "client_reference": str(uuid4()),
        "dispatch_id": dispatch["dispatch_id"],
        "scope_version_id": scope_id,
        "completion_media_asset_ids": media_ids,
        "completed_check_keys": ["tools_removed", "site_restored"],
        "worker_shifts": [
            {
                "worker_id": setup["worker_options"][0]["id"],
                "started_at": checked_in_at.isoformat(),
                "ended_at": completed_at.isoformat(),
            }
        ],
        "onsite_customer_confirmed": True,
        "onsite_confirmed_at": completed_at.isoformat(),
        "work_ended_at": completed_at.isoformat(),
    }
    return created, payload


@pytest.mark.anyio
async def test_completion_happy_path_documents_notifications_and_retention(
    completion_lifecycle_api: CompletionApi,
) -> None:
    client, factory, _ = completion_lifecycle_api
    created, submission_payload = await _context(completion_lifecycle_api, with_media=True)
    job_id = created["job"]["id"]
    submission_url = f"/api/v1/move-jobs/{job_id}/completion-submissions"
    submitted = await client.post(
        submission_url,
        headers=_headers(created, "field_worker"),
        json=submission_payload,
    )
    assert submitted.status_code == 201
    submission = submitted.json()
    assert (
        submission["completion_media_asset_ids"] == submission_payload["completion_media_asset_ids"]
    )
    assert (
        await client.post(
            submission_url,
            headers=_headers(created, "field_worker"),
            json=submission_payload,
        )
    ).json() == submission

    brief = await client.get(
        f"/api/v1/move-jobs/{job_id}/field-brief",
        headers=_headers(created, "field_worker"),
    )
    assert brief.status_code == 200
    assert brief.json()["completion_submission_id"] == submission["completion_submission_id"]
    assert brief.json()["completion_required_count"] == 0
    assert all(item["confirmed"] for item in brief.json()["completion_check_items"])
    assert brief.json()["assigned_workers"][0]["is_lead"] is True

    summary_url = f"/api/v1/move-jobs/{job_id}/completion-summary"
    provider_summary = await client.get(
        summary_url,
        headers=_headers(created, "company_manager"),
    )
    assert provider_summary.status_code == 200
    assert provider_summary.headers["cache-control"] == "no-store"
    summary = provider_summary.json()
    assert summary["final_amount_krw"] == 550_000
    assert summary["completion_media_count"] == 1
    assert summary["checklist"] == {"completed_count": 2, "total_count": 2}
    assert summary["archive_ready"] is True
    assert {item["status"] for item in summary["documents"]} == {"ready"}
    summary_with_change = CompletionSummaryView.model_validate(summary, strict=False).model_copy(
        update={
            "field_changes": (
                CompletionFieldChangeSummary(
                    proposal_id=uuid4(),
                    title="사다리차 추가",
                    status="approved",
                    amount_delta_krw=100_000,
                    total_amount_krw=650_000,
                    decided_at=None,
                ),
            )
        }
    )
    assert completion_documents.build_completion_archive(summary_with_change).startswith(b"PK")
    assert (await client.get(summary_url, headers=_headers(created, "customer"))).status_code == 409

    archive_url = f"/api/v1/move-jobs/{job_id}/documents/archive"
    archive = await client.get(
        archive_url,
        headers=_headers(created, "company_manager"),
    )
    archive_replay = await client.get(
        archive_url,
        headers=_headers(created, "company_manager"),
    )
    assert archive.status_code == 200
    assert archive.headers["content-type"] == "application/zip"
    assert archive.headers["cache-control"] == "no-store"
    assert archive.content == archive_replay.content
    with zipfile.ZipFile(io.BytesIO(archive.content)) as package:
        assert set(package.namelist()) == {
            "01_견적서.pdf",
            "02_변경_승인_기록.pdf",
            "03_작업_완료_기록.pdf",
            "04_완료_확인_기록.pdf",
            "manifest.json",
        }
        assert all(
            package.read(name).startswith(b"%PDF-1.4")
            for name in package.namelist()
            if name.endswith(".pdf")
        )
        assert b"storage.invalid" not in archive.content

    request_payload = {
        "client_reference": str(uuid4()),
        "completion_submission_id": submission["completion_submission_id"],
    }
    request_url = f"/api/v1/move-jobs/{job_id}/completion-requests"
    requested = await client.post(
        request_url,
        headers=_headers(created, "company_manager"),
        json=request_payload,
    )
    assert requested.status_code == 201
    assert requested.json()["status"] == "requested"
    assert requested.json()["notification_created"] is True
    assert (
        await client.post(
            request_url,
            headers=_headers(created, "company_manager"),
            json=request_payload,
        )
    ).json() == requested.json()
    customer_summary = await client.get(
        summary_url,
        headers=_headers(created, "customer"),
    )
    assert customer_summary.status_code == 200
    assert customer_summary.json()["completion_request"]["status"] == "requested"
    pending_archive = await client.get(
        archive_url,
        headers=_headers(created, "company_manager"),
    )
    assert pending_archive.status_code == 200

    decision_url = f"{request_url}/{requested.json()['completion_request_id']}/decision"
    decision_payload = {
        "decision": "confirm",
        "unrecorded_extra_charge": False,
    }
    decided = await client.post(
        decision_url,
        headers=_headers(created, "customer"),
        json=decision_payload,
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "confirmed"
    assert decided.json()["job_status"] == "completed"
    assert decided.json()["retention_scheduled_count"] == 1
    assert (
        await client.post(
            decision_url,
            headers=_headers(created, "customer"),
            json=decision_payload,
        )
    ).json() == decided.json()
    confirmed_archive = await client.get(
        archive_url,
        headers=_headers(created, "company_manager"),
    )
    assert confirmed_archive.status_code == 200
    assert (
        await client.post(
            submission_url,
            headers=_headers(created, "field_worker"),
            json={**submission_payload, "client_reference": str(uuid4())},
        )
    ).status_code == 409
    assert (
        await client.post(
            request_url,
            headers=_headers(created, "company_manager"),
            json={**request_payload, "client_reference": str(uuid4())},
        )
    ).status_code == 409
    assert (
        await client.post(
            submission_url,
            headers=_headers(created, "field_worker"),
            json=submission_payload,
        )
    ).json() == submission

    async with factory.begin() as session:
        assert len((await session.scalars(select(CompletionConfirmation))).all()) == 2
        background_jobs = (await session.scalars(select(BackgroundJob))).all()
        assert len(background_jobs) == 1
        lifecycle_events = (
            await session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.event_type.in_(
                        {
                            DomainEventType.COMPLETION_SUBMITTED_V1,
                            DomainEventType.COMPLETION_REQUESTED_V1,
                            DomainEventType.COMPLETION_DECIDED_V1,
                        }
                    )
                )
            )
        ).all()
        assert len(lifecycle_events) == 3
        for event in lifecycle_events:
            assert len(await consume_notification_event(session, _to_domain_event(event))) == 1
            assert await consume_notification_event(session, _to_domain_event(event)) == ()

    worker_notifications = await client.get(
        f"/api/v1/move-jobs/{job_id}/notifications",
        headers=_headers(created, "field_worker"),
    )
    assert not {item["event_type"] for item in worker_notifications.json()} & {
        "completion_submitted.v1",
        "completion_requested.v1",
        "completion_decided.v1",
    }
    manager_notifications = await client.get(
        f"/api/v1/move-jobs/{job_id}/notifications",
        headers=_headers(created, "company_manager"),
    )
    assert {item["event_type"] for item in manager_notifications.json()} == {
        "completion_submitted.v1",
        "completion_decided.v1",
    }
    customer_notifications = await client.get(
        f"/api/v1/move-jobs/{job_id}/notifications",
        headers=_headers(created, "customer"),
    )
    assert {item["event_type"] for item in customer_notifications.json()} == {
        "completion_requested.v1"
    }
    final_summary = await client.get(
        summary_url,
        headers=_headers(created, "company_manager"),
    )
    assert final_summary.json()["job_status"] == "completed"
    assert final_summary.json()["retention_until"] is not None


@pytest.mark.anyio
async def test_problem_report_allows_corrected_submission_and_final_confirmation(
    completion_lifecycle_api: CompletionApi,
) -> None:
    client, factory, _ = completion_lifecycle_api
    created, payload = await _context(completion_lifecycle_api, with_media=False)
    job_id = created["job"]["id"]
    submission_url = f"/api/v1/move-jobs/{job_id}/completion-submissions"
    first = await client.post(
        submission_url,
        headers=_headers(created, "field_worker"),
        json=payload,
    )
    assert first.status_code == 201
    request_url = f"/api/v1/move-jobs/{job_id}/completion-requests"
    request = await client.post(
        request_url,
        headers=_headers(created, "company_manager"),
        json={
            "client_reference": str(uuid4()),
            "completion_submission_id": first.json()["completion_submission_id"],
        },
    )
    assert request.status_code == 201
    decision_url = f"{request_url}/{request.json()['completion_request_id']}/decision"
    report_payload = {
        "decision": "report_issue",
        "problem_type": "missing_work",
        "problem_description": "주방 포장재 회수가 누락됐습니다.",
        "unrecorded_extra_charge": None,
    }
    reported = await client.post(
        decision_url,
        headers=_headers(created, "customer"),
        json=report_payload,
    )
    assert reported.status_code == 200
    assert reported.json()["status"] == "issue_reported"
    assert reported.json()["job_status"] == "draft"
    assert reported.json()["problem_report"]["problem_type"] == "missing_work"
    problem_archive = await client.get(
        f"/api/v1/move-jobs/{job_id}/documents/archive",
        headers=_headers(created, "company_manager"),
    )
    assert problem_archive.status_code == 200
    assert (
        await client.post(
            decision_url,
            headers=_headers(created, "customer"),
            json=report_payload,
        )
    ).json() == reported.json()
    conflicting = await client.post(
        decision_url,
        headers=_headers(created, "customer"),
        json={
            "decision": "report_issue",
            "problem_type": "damage",
            "problem_description": "다른 신고",
        },
    )
    assert conflicting.status_code == 409

    corrected_payload = {**payload, "client_reference": str(uuid4())}
    corrected = await client.post(
        submission_url,
        headers=_headers(created, "field_worker"),
        json=corrected_payload,
    )
    assert corrected.status_code == 201
    assert (
        await client.get(
            f"/api/v1/move-jobs/{job_id}/completion-summary",
            headers=_headers(created, "customer"),
        )
    ).status_code == 409
    corrected_provider_summary = await client.get(
        f"/api/v1/move-jobs/{job_id}/completion-summary",
        headers=_headers(created, "company_manager"),
    )
    assert corrected_provider_summary.status_code == 200
    assert corrected_provider_summary.json()["completion_request"] is None
    assert corrected_provider_summary.json()["problem_report_count"] == 1
    assert (
        await client.post(
            request_url,
            headers=_headers(created, "company_manager"),
            json={
                "client_reference": str(uuid4()),
                "completion_submission_id": first.json()["completion_submission_id"],
            },
        )
    ).status_code == 409
    second_request = await client.post(
        request_url,
        headers=_headers(created, "company_manager"),
        json={
            "client_reference": str(uuid4()),
            "completion_submission_id": corrected.json()["completion_submission_id"],
        },
    )
    assert second_request.status_code == 201
    confirmed = await client.post(
        f"{request_url}/{second_request.json()['completion_request_id']}/decision",
        headers=_headers(created, "customer"),
        json={"decision": "confirm", "unrecorded_extra_charge": True},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["job_status"] == "completed"
    assert confirmed.json()["retention_scheduled_count"] == 0
    assert (
        await client.get(
            f"/api/v1/move-jobs/{job_id}/documents/archive",
            headers=_headers(created, "company_manager"),
        )
    ).status_code == 200
    async with factory() as session:
        assert len((await session.scalars(select(CompletionProblemReport))).all()) == 1
        assert len((await session.scalars(select(CompletionSubmission))).all()) == 2
        assert len((await session.scalars(select(CompletionRequest))).all()) == 2


@pytest.mark.anyio
async def test_expired_and_revoked_requests_are_visible_but_not_decidable(
    completion_lifecycle_api: CompletionApi,
) -> None:
    client, factory, _ = completion_lifecycle_api
    created, payload = await _context(completion_lifecycle_api, with_media=False)
    job_id = created["job"]["id"]
    submission = await client.post(
        f"/api/v1/move-jobs/{job_id}/completion-submissions",
        headers=_headers(created, "field_worker"),
        json=payload,
    )
    request_url = f"/api/v1/move-jobs/{job_id}/completion-requests"
    expired = await client.post(
        request_url,
        headers=_headers(created, "company_manager"),
        json={
            "client_reference": str(uuid4()),
            "completion_submission_id": submission.json()["completion_submission_id"],
        },
    )
    expired_id = expired.json()["completion_request_id"]
    async with factory.begin() as session:
        row = await session.get(CompletionRequest, UUID(expired_id))
        assert row is not None
        row.requested_at = datetime.now(UTC) - timedelta(days=9)
        row.expires_at = datetime.now(UTC) - timedelta(days=2)
    summary = await client.get(
        f"/api/v1/move-jobs/{job_id}/completion-summary",
        headers=_headers(created, "customer"),
    )
    assert summary.status_code == 200
    assert summary.json()["completion_request"]["status"] == "expired"
    expired_decision = await client.post(
        f"{request_url}/{expired_id}/decision",
        headers=_headers(created, "customer"),
        json={"decision": "confirm"},
    )
    assert expired_decision.status_code == 409

    active = await client.post(
        request_url,
        headers=_headers(created, "company_manager"),
        json={
            "client_reference": str(uuid4()),
            "completion_submission_id": submission.json()["completion_submission_id"],
        },
    )
    assert active.status_code == 201
    revoke_url = f"{request_url}/{active.json()['completion_request_id']}/revoke"
    revoked = await client.post(
        revoke_url,
        headers=_headers(created, "company_manager"),
        json={"reason": "고객 요청으로 다시 점검"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert (
        await client.post(
            revoke_url,
            headers=_headers(created, "company_manager"),
            json={"reason": "고객 요청으로 다시 점검"},
        )
    ).json() == revoked.json()
    assert (
        await client.post(
            f"{request_url}/{active.json()['completion_request_id']}/decision",
            headers=_headers(created, "customer"),
            json={"decision": "confirm"},
        )
    ).status_code == 409
    assert (
        await client.post(
            revoke_url,
            headers=_headers(created, "company_manager"),
            json={"reason": "다른 이유"},
        )
    ).status_code == 409


@pytest.mark.anyio
async def test_completion_authorization_validation_and_not_ready_states(
    completion_lifecycle_api: CompletionApi,
) -> None:
    client, _, _ = completion_lifecycle_api
    created, payload = await _context(completion_lifecycle_api, with_media=False)
    job_id = created["job"]["id"]
    submission_url = f"/api/v1/move-jobs/{job_id}/completion-submissions"
    summary_url = f"/api/v1/move-jobs/{job_id}/completion-summary"
    archive_url = f"/api/v1/move-jobs/{job_id}/documents/archive"

    empty_summary = await client.get(
        summary_url,
        headers=_headers(created, "company_manager"),
    )
    assert empty_summary.status_code == 200
    assert empty_summary.json()["archive_ready"] is False
    assert {item["status"] for item in empty_summary.json()["documents"]} == {"not_ready"}
    assert (
        await client.get(archive_url, headers=_headers(created, "company_manager"))
    ).status_code == 409
    assert (await client.get(summary_url, headers=_headers(created, "customer"))).status_code == 409
    assert (
        await client.get(summary_url, headers=_headers(created, "field_worker"))
    ).status_code == 403
    assert (
        await client.post(
            submission_url,
            headers=_headers(created, "company_manager"),
            json=payload,
        )
    ).status_code == 403

    incomplete = {**payload, "completed_check_keys": ["tools_removed"]}
    assert (
        await client.post(
            submission_url,
            headers=_headers(created, "field_worker"),
            json=incomplete,
        )
    ).status_code == 409
    wrong_dispatch = {**payload, "client_reference": str(uuid4()), "dispatch_id": str(uuid4())}
    assert (
        await client.post(
            submission_url,
            headers=_headers(created, "field_worker"),
            json=wrong_dispatch,
        )
    ).status_code == 404
    wrong_scope = {**payload, "client_reference": str(uuid4()), "scope_version_id": str(uuid4())}
    assert (
        await client.post(
            submission_url,
            headers=_headers(created, "field_worker"),
            json=wrong_scope,
        )
    ).status_code == 409
    future = datetime.now(UTC) + timedelta(minutes=10)
    future_payload = {
        **payload,
        "client_reference": str(uuid4()),
        "onsite_confirmed_at": future.isoformat(),
        "work_ended_at": future.isoformat(),
    }
    assert (
        await client.post(
            submission_url,
            headers=_headers(created, "field_worker"),
            json=future_payload,
        )
    ).status_code == 409

    capture = await client.post(
        f"/api/v1/move-jobs/{job_id}/capture-sessions",
        headers=_headers(created, "field_worker"),
    )
    upload = await client.post(
        f"/api/v1/move-jobs/{job_id}/capture-sessions/{capture.json()['id']}/media-assets/upload",
        headers=_headers(created, "field_worker"),
        json={
            "room_zone_id": created["job"]["locations"][0]["room_zones"][0]["id"],
            "media_purpose": "completion",
            "content_type": "image/jpeg",
            "content_length": 10,
        },
    )
    not_ready = {
        **payload,
        "client_reference": str(uuid4()),
        "completion_media_asset_ids": [upload.json()["asset"]["id"]],
    }
    assert (
        await client.post(
            submission_url,
            headers=_headers(created, "field_worker"),
            json=not_ready,
        )
    ).status_code == 422

    submitted = await client.post(
        submission_url,
        headers=_headers(created, "field_worker"),
        json=payload,
    )
    assert submitted.status_code == 201
    replay_conflict = {
        **payload,
        "completed_check_keys": list(reversed(payload["completed_check_keys"])),
    }
    assert (
        await client.post(
            submission_url,
            headers=_headers(created, "field_worker"),
            json=replay_conflict,
        )
    ).status_code == 409
    conflict_payload = {**payload, "client_reference": str(uuid4())}
    assert (
        await client.post(
            submission_url,
            headers=_headers(created, "field_worker"),
            json=conflict_payload,
        )
    ).status_code == 409
    request_url = f"/api/v1/move-jobs/{job_id}/completion-requests"
    assert (
        await client.post(
            request_url,
            headers=_headers(created, "field_worker"),
            json={
                "client_reference": str(uuid4()),
                "completion_submission_id": submitted.json()["completion_submission_id"],
            },
        )
    ).status_code == 403
    assert (
        await client.post(
            request_url,
            headers=_headers(created, "company_manager"),
            json={
                "client_reference": str(uuid4()),
                "completion_submission_id": str(uuid4()),
            },
        )
    ).status_code == 404
    request_payload = {
        "client_reference": str(uuid4()),
        "completion_submission_id": submitted.json()["completion_submission_id"],
    }
    request = await client.post(
        request_url,
        headers=_headers(created, "company_manager"),
        json=request_payload,
    )
    assert request.status_code == 201
    assert (
        await client.post(
            request_url,
            headers=_headers(created, "company_manager"),
            json={**request_payload, "completion_submission_id": str(uuid4())},
        )
    ).status_code == 409
    assert (
        await client.post(
            request_url,
            headers=_headers(created, "company_manager"),
            json={**request_payload, "client_reference": str(uuid4())},
        )
    ).status_code == 409
    assert (
        await client.post(
            f"{request_url}/{uuid4()}/revoke",
            headers=_headers(created, "company_manager"),
            json={"reason": "없는 요청"},
        )
    ).status_code == 404
    decision_url = f"{request_url}/{request.json()['completion_request_id']}/decision"
    assert (
        await client.post(
            decision_url,
            headers=_headers(created, "company_manager"),
            json={"decision": "confirm"},
        )
    ).status_code == 403
    assert (
        await client.post(
            decision_url,
            headers=_headers(created, "customer"),
            json={"decision": "report_issue"},
        )
    ).status_code == 422
    assert (
        await client.post(
            f"{request_url}/{uuid4()}/decision",
            headers=_headers(created, "customer"),
            json={"decision": "confirm"},
        )
    ).status_code == 404
    assert (
        await client.post(
            f"{request_url}/{request.json()['completion_request_id']}/revoke",
            headers=_headers(created, "customer"),
            json={"reason": "권한 없음"},
        )
    ).status_code == 403


@pytest.mark.parametrize(
    "value",
    (
        " https://storage.invalid/read/object",
        "http://storage.invalid/read/object",
        "https:///read/object",
        "https://storage.invalid:invalid/read/object",
    ),
)
def test_completion_rejects_invalid_storage_read_urls(value: str) -> None:
    with pytest.raises(ProviderError, match="invalid completion read URL"):
        completion_service._validated_read_url(value)


@pytest.mark.anyio
async def test_completion_rejects_missing_or_corrupt_dispatch_and_media_state(
    completion_lifecycle_api: CompletionApi,
) -> None:
    client, factory, _ = completion_lifecycle_api

    no_setup = await _create_job(client)
    no_setup_scope = await _confirmed_scope(client, no_setup)
    now = datetime.now(UTC)
    no_setup_payload = {
        "client_reference": str(uuid4()),
        "dispatch_id": str(uuid4()),
        "scope_version_id": no_setup_scope,
        "completion_media_asset_ids": [],
        "completed_check_keys": ["done"],
        "worker_shifts": [
            {
                "worker_id": str(uuid4()),
                "started_at": now.isoformat(),
                "ended_at": now.isoformat(),
            }
        ],
        "onsite_customer_confirmed": True,
        "onsite_confirmed_at": now.isoformat(),
        "work_ended_at": now.isoformat(),
    }
    assert (
        await client.post(
            f"/api/v1/move-jobs/{no_setup['job']['id']}/completion-submissions",
            headers=_headers(no_setup, "field_worker"),
            json=no_setup_payload,
        )
    ).status_code == 404

    missing_check_in, missing_check_in_payload = await _context(
        completion_lifecycle_api,
        with_media=False,
    )
    missing_check_in_job_id = UUID(missing_check_in["job"]["id"])
    async with factory.begin() as session:
        check_in = await session.scalar(
            select(FieldCheckIn).where(FieldCheckIn.job_id == missing_check_in_job_id)
        )
        assert check_in is not None
        await session.delete(check_in)
    assert (
        await client.post(
            f"/api/v1/move-jobs/{missing_check_in_job_id}/completion-submissions",
            headers=_headers(missing_check_in, "field_worker"),
            json=missing_check_in_payload,
        )
    ).status_code == 409

    wrong_worker, wrong_worker_payload = await _context(
        completion_lifecycle_api,
        with_media=False,
    )
    wrong_worker_payload["worker_shifts"][0]["worker_id"] = str(uuid4())
    assert (
        await client.post(
            f"/api/v1/move-jobs/{wrong_worker['job']['id']}/completion-submissions",
            headers=_headers(wrong_worker, "field_worker"),
            json=wrong_worker_payload,
        )
    ).status_code == 409

    stale_plan, stale_plan_payload = await _context(
        completion_lifecycle_api,
        with_media=False,
    )
    stale_job_id = UUID(stale_plan["job"]["id"])
    async with factory.begin() as session:
        plan = await session.get(DispatchPlan, UUID(stale_plan_payload["dispatch_id"]))
        assert plan is not None
        scopes = (
            await session.scalars(
                select(ScopeVersion)
                .where(ScopeVersion.job_id == stale_job_id)
                .order_by(ScopeVersion.sequence_number)
            )
        ).all()
        assert len(scopes) == 2
        plan.source_scope_version_id = scopes[0].id
    assert (
        await client.post(
            f"/api/v1/move-jobs/{stale_job_id}/completion-submissions",
            headers=_headers(stale_plan, "field_worker"),
            json=stale_plan_payload,
        )
    ).status_code == 409

    wrong_lead, wrong_lead_payload = await _context(
        completion_lifecycle_api,
        with_media=False,
    )
    wrong_lead_job_id = UUID(wrong_lead["job"]["id"])
    async with factory.begin() as session:
        setup = await session.scalar(
            select(DispatchSetup).where(DispatchSetup.job_id == wrong_lead_job_id)
        )
        assert setup is not None
        worker_options = [dict(option) for option in setup.worker_options]
        worker_options[0]["participant_id"] = str(uuid4())
        setup.worker_options = worker_options
    assert (
        await client.post(
            f"/api/v1/move-jobs/{wrong_lead_job_id}/completion-submissions",
            headers=_headers(wrong_lead, "field_worker"),
            json=wrong_lead_payload,
        )
    ).status_code == 409

    corrupt_response, corrupt_response_payload = await _context(
        completion_lifecycle_api,
        with_media=False,
    )
    corrupt_job_id = UUID(corrupt_response["job"]["id"])
    submitted = await client.post(
        f"/api/v1/move-jobs/{corrupt_job_id}/completion-submissions",
        headers=_headers(corrupt_response, "field_worker"),
        json=corrupt_response_payload,
    )
    assert submitted.status_code == 201
    async with factory() as session:
        submission = await session.get(
            CompletionSubmission,
            UUID(submitted.json()["completion_submission_id"]),
        )
        setup = await session.scalar(
            select(DispatchSetup).where(DispatchSetup.job_id == corrupt_job_id)
        )
        assert submission is not None and setup is not None
        submission.worker_shifts = [
            {
                **dict(submission.worker_shifts[0]),
                "worker_id": str(uuid4()),
            }
        ]
        with pytest.raises(completion_service.CompletionConflictError):
            await completion_service._submission_response(session, submission, setup)

    stale_media, stale_media_payload = await _context(
        completion_lifecycle_api,
        with_media=True,
    )
    stale_media_job_id = stale_media["job"]["id"]
    stale_media_submission = await client.post(
        f"/api/v1/move-jobs/{stale_media_job_id}/completion-submissions",
        headers=_headers(stale_media, "field_worker"),
        json=stale_media_payload,
    )
    assert stale_media_submission.status_code == 201
    async with factory.begin() as session:
        asset = await session.get(
            MediaAsset,
            UUID(stale_media_payload["completion_media_asset_ids"][0]),
        )
        assert asset is not None
        asset.status = MediaAssetStatus.UPLOADED
    assert (
        await client.get(
            f"/api/v1/move-jobs/{stale_media_job_id}/completion-summary",
            headers=_headers(stale_media, "company_manager"),
        )
    ).status_code == 409


@pytest.mark.anyio
async def test_completion_maps_integrity_races_and_reuses_exact_confirmations(
    completion_lifecycle_api: CompletionApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory, _ = completion_lifecycle_api
    created, payload = await _context(completion_lifecycle_api, with_media=False)
    job_id = UUID(created["job"]["id"])
    worker_id = UUID(_participant_id(created, "field_worker"))
    manager_id = UUID(_participant_id(created, "company_manager"))
    customer_id = UUID(_participant_id(created, "customer"))
    submission_command = CompletionSubmissionCreate.model_validate(payload)

    async with factory() as session:
        monkeypatch.setattr(
            session,
            "flush",
            AsyncMock(side_effect=IntegrityError("completion", {}, RuntimeError("race"))),
        )
        with pytest.raises(completion_service.CompletionConflictError):
            await completion_service.submit_completion(
                session,
                job_id,
                worker_id,
                submission_command,
                trace_id="0" * 32,
            )
        await session.rollback()

    submission = await client.post(
        f"/api/v1/move-jobs/{job_id}/completion-submissions",
        headers=_headers(created, "field_worker"),
        json=payload,
    )
    assert submission.status_code == 201
    submission_id = UUID(submission.json()["completion_submission_id"])
    request_command = CompletionRequestCreate(
        client_reference=uuid4(),
        completion_submission_id=submission_id,
    )
    async with factory() as session:
        monkeypatch.setattr(
            session,
            "flush",
            AsyncMock(side_effect=IntegrityError("request", {}, RuntimeError("race"))),
        )
        with pytest.raises(completion_service.CompletionConflictError):
            await completion_service.create_completion_request(
                session,
                job_id,
                manager_id,
                request_command,
                trace_id="0" * 32,
            )
        await session.rollback()

    requested = await client.post(
        f"/api/v1/move-jobs/{job_id}/completion-requests",
        headers=_headers(created, "company_manager"),
        json=request_command.model_dump(mode="json"),
    )
    assert requested.status_code == 201
    request_id = UUID(requested.json()["completion_request_id"])
    decision_command = CompletionDecisionCreate(
        decision="report_issue",
        problem_type=CompletionProblemType.OTHER,
        problem_description="합성 무결성 경합",
    )
    async with factory() as session:
        request = await session.get(CompletionRequest, request_id)
        assert request is not None
        with pytest.raises(completion_service.CompletionConflictError):
            await completion_service._decision_response(
                session,
                cast(
                    Any,
                    SimpleNamespace(id=job_id, status=MoveJobStatus.DRAFT, completed_at=None),
                ),
                request,
            )
        monkeypatch.setattr(
            session,
            "flush",
            AsyncMock(side_effect=IntegrityError("decision", {}, RuntimeError("race"))),
        )
        with pytest.raises(completion_service.CompletionConflictError):
            await completion_service.decide_completion_request(
                session,
                job_id,
                request_id,
                customer_id,
                decision_command,
                retention_days=30,
                trace_id="0" * 32,
            )
        await session.rollback()

    confirmed = await client.post(
        f"/api/v1/move-jobs/{job_id}/completion-requests/{request_id}/decision",
        headers=_headers(created, "customer"),
        json={"decision": "confirm"},
    )
    assert confirmed.status_code == 200
    async with factory() as session:
        manager_confirmation = await session.scalar(
            select(CompletionConfirmation).where(
                CompletionConfirmation.job_id == job_id,
                CompletionConfirmation.role == ParticipantRole.COMPANY_MANAGER,
            )
        )
        assert manager_confirmation is not None
        evidence_ids = tuple(
            (
                await session.scalars(
                    select(CompletionEvidence.media_asset_id).where(
                        CompletionEvidence.confirmation_id == manager_confirmation.id
                    )
                )
            ).all()
        )
        reused = await completion_service._ensure_completion_confirmation(
            session,
            job_id,
            manager_confirmation.scope_version_id,
            manager_id,
            ParticipantRole.COMPANY_MANAGER,
            evidence_ids,
            confirmed_at=datetime.now(UTC),
        )
        assert reused.id == manager_confirmation.id
        with pytest.raises(completion_service.CompletionConflictError):
            await completion_service._ensure_completion_confirmation(
                session,
                job_id,
                manager_confirmation.scope_version_id,
                uuid4(),
                ParticipantRole.COMPANY_MANAGER,
                evidence_ids,
                confirmed_at=datetime.now(UTC),
            )


@pytest.mark.anyio
async def test_completion_defensive_invariants_reject_corrupt_relations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock(spec=AsyncSession)
    job_id = uuid4()
    now = datetime.now(UTC)

    session.scalar = AsyncMock(return_value=None)
    with pytest.raises(completion_service.CompletionResourceNotFoundError):
        await completion_service._load_completion_job(session, job_id)
    with pytest.raises(completion_service.CompletionResourceNotFoundError):
        completion_service._participant(
            cast(Any, SimpleNamespace(id=job_id, participants=[])),
            uuid4(),
            ParticipantRole.CUSTOMER,
        )
    with pytest.raises(completion_service.CompletionResourceNotFoundError):
        await completion_service._current_locked_scope(session, job_id)

    locked_scope = SimpleNamespace(id=uuid4(), locked_at=now)
    session.scalar = AsyncMock(side_effect=[locked_scope, uuid4()])
    with pytest.raises(completion_service.CompletionConflictError):
        await completion_service._current_locked_scope(session, job_id)

    manager_id = uuid4()
    customer_id = uuid4()
    manager = SimpleNamespace(
        id=manager_id,
        role=ParticipantRole.COMPANY_MANAGER,
        display_name="업체",
    )
    customer = SimpleNamespace(
        id=customer_id,
        role=ParticipantRole.CUSTOMER,
        display_name="고객",
    )
    job = SimpleNamespace(
        id=job_id,
        status=MoveJobStatus.DRAFT,
        completed_at=None,
        participants=[manager],
        locations=[],
    )
    submission = SimpleNamespace(
        id=uuid4(),
        job_id=job_id,
        scope_version_id=locked_scope.id,
    )
    request_command = CompletionRequestCreate(
        client_reference=uuid4(),
        completion_submission_id=submission.id,
    )
    monkeypatch.setattr(
        completion_service,
        "_load_completion_job",
        AsyncMock(return_value=job),
    )
    monkeypatch.setattr(
        completion_service,
        "_latest_submission",
        AsyncMock(return_value=submission),
    )
    monkeypatch.setattr(completion_service, "_current_locked_scope", AsyncMock())
    monkeypatch.setattr(
        completion_service,
        "_latest_request",
        AsyncMock(return_value=None),
    )
    session.scalar = AsyncMock(side_effect=[None, submission])
    with pytest.raises(completion_service.CompletionResourceNotFoundError):
        await completion_service.create_completion_request(
            session,
            job_id,
            manager_id,
            request_command,
            trace_id="0" * 32,
        )

    job.participants = [manager, customer]
    request = SimpleNamespace(
        id=uuid4(),
        job_id=job_id,
        completion_submission_id=submission.id,
        requested_by_participant_id=manager_id,
        status=CompletionRequestStatus.REQUESTED,
        expires_at=now + timedelta(days=1),
    )
    decision = CompletionDecisionCreate(decision="confirm")
    monkeypatch.setattr(
        completion_service,
        "_latest_request",
        AsyncMock(return_value=request),
    )
    monkeypatch.setattr(
        completion_service,
        "_latest_submission",
        AsyncMock(return_value=None),
    )
    session.scalar = AsyncMock(side_effect=[request, None])
    with pytest.raises(completion_service.CompletionResourceNotFoundError):
        await completion_service.decide_completion_request(
            session,
            job_id,
            request.id,
            customer_id,
            decision,
            retention_days=30,
            trace_id="0" * 32,
        )

    other_submission = SimpleNamespace(id=uuid4())
    monkeypatch.setattr(
        completion_service,
        "_latest_submission",
        AsyncMock(return_value=other_submission),
    )
    session.scalar = AsyncMock(side_effect=[request, submission])
    with pytest.raises(completion_service.CompletionConflictError):
        await completion_service.decide_completion_request(
            session,
            job_id,
            request.id,
            customer_id,
            decision,
            retention_days=30,
            trace_id="0" * 32,
        )

    monkeypatch.setattr(
        completion_service,
        "_latest_submission",
        AsyncMock(return_value=submission),
    )
    monkeypatch.setattr(
        completion_service,
        "_submission_evidence_ids",
        AsyncMock(return_value=(uuid4(),)),
    )
    monkeypatch.setattr(
        completion_service,
        "_ensure_completion_confirmation",
        AsyncMock(),
    )
    session.scalar = AsyncMock(side_effect=[request, submission])
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: []))
    with pytest.raises(completion_service.CompletionConflictError):
        await completion_service.decide_completion_request(
            session,
            job_id,
            request.id,
            customer_id,
            decision,
            retention_days=30,
            trace_id="0" * 32,
        )

    missing_zone_asset = SimpleNamespace(
        id=uuid4(),
        generation="7",
        room_zone_id=uuid4(),
        object_key="completion/missing-zone.jpg",
        content_type="image/jpeg",
        created_at=now,
    )
    monkeypatch.setattr(
        completion_service,
        "_submission_evidence_ids",
        AsyncMock(return_value=(missing_zone_asset.id,)),
    )
    session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: [missing_zone_asset]))
    with pytest.raises(completion_service.CompletionConflictError):
        await completion_service._completion_media_previews(
            session,
            FakeObjectStorage(),
            cast(Any, SimpleNamespace(locations=[])),
            submission.id,
        )

    field_worker = SimpleNamespace(
        id=uuid4(),
        role=ParticipantRole.FIELD_WORKER,
        display_name="기사",
    )
    job.participants = [field_worker]
    monkeypatch.setattr(
        completion_service,
        "_load_completion_job",
        AsyncMock(return_value=job),
    )
    with pytest.raises(completion_service.CompletionResourceNotFoundError):
        await completion_service.get_completion_summary(
            session,
            FakeObjectStorage(),
            job_id,
            field_worker.id,
            ParticipantRole.FIELD_WORKER,
        )

    job.participants = [manager]
    monkeypatch.setattr(
        completion_service,
        "_load_completion_job",
        AsyncMock(return_value=job),
    )
    monkeypatch.setattr(
        completion_service,
        "_latest_submission",
        AsyncMock(return_value=submission),
    )
    monkeypatch.setattr(
        completion_service,
        "_latest_request",
        AsyncMock(return_value=None),
    )
    session.scalar = AsyncMock(side_effect=[locked_scope, None, None])
    with pytest.raises(completion_service.CompletionConflictError):
        await completion_service.get_completion_summary(
            session,
            FakeObjectStorage(),
            job_id,
            manager_id,
            ParticipantRole.COMPANY_MANAGER,
        )

    session.scalar = AsyncMock(side_effect=[None, None])
    assert await completion_service._quote_for_scope(session, job_id, locked_scope.id) is None
    session.scalar = AsyncMock(
        side_effect=[
            None,
            SimpleNamespace(
                base_amount_krw=550_000,
                adjustments=[{"label": "사다리차", "amount_krw": 100_000}],
                total_amount_krw=650_000,
            ),
        ]
    )
    fallback_quote = await completion_service._quote_for_scope(
        session,
        job_id,
        locked_scope.id,
    )
    assert fallback_quote is not None
    assert fallback_quote.total_amount_krw == 650_000

    setup = SimpleNamespace(completion_check_items=[])
    cross_job_scope = SimpleNamespace(id=locked_scope.id, job_id=uuid4(), sequence_number=2)
    monkeypatch.setattr(
        completion_service,
        "_submission_response",
        AsyncMock(return_value=SimpleNamespace(worker_shifts=())),
    )
    session.scalar = AsyncMock(side_effect=[locked_scope, None, setup])
    session.get = AsyncMock(return_value=cross_job_scope)
    with pytest.raises(completion_service.CompletionConflictError):
        await completion_service.get_completion_summary(
            session,
            FakeObjectStorage(),
            job_id,
            manager_id,
            ParticipantRole.COMPANY_MANAGER,
        )


@pytest.mark.anyio
async def test_completion_router_maps_service_and_provider_failures(
    completion_lifecycle_api: CompletionApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, factory, storage = completion_lifecycle_api
    created = await _create_job(client)
    job_id = created["job"]["id"]
    manager_headers = _headers(created, "company_manager")
    customer_headers = _headers(created, "customer")
    summary_url = f"/api/v1/move-jobs/{job_id}/completion-summary"
    archive_url = f"/api/v1/move-jobs/{job_id}/documents/archive"
    request_url = f"/api/v1/move-jobs/{job_id}/completion-requests"

    monkeypatch.setattr(
        completion_router,
        "get_completion_summary",
        AsyncMock(side_effect=completion_service.CompletionResourceNotFoundError(job_id)),
    )
    assert (await client.get(summary_url, headers=manager_headers)).status_code == 404
    assert (await client.get(archive_url, headers=manager_headers)).status_code == 404

    provider_error = ProviderError(
        ProviderErrorKind.UNAVAILABLE,
        "synthetic storage outage",
        retryable=True,
    )
    monkeypatch.setattr(
        completion_router,
        "get_completion_summary",
        AsyncMock(side_effect=provider_error),
    )
    assert (await client.get(summary_url, headers=manager_headers)).status_code == 503
    assert (await client.get(archive_url, headers=manager_headers)).status_code == 503

    monkeypatch.setattr(
        completion_router,
        "create_completion_request",
        AsyncMock(side_effect=completion_service.CompletionConflictError(job_id)),
    )
    assert (
        await client.post(
            request_url,
            headers=manager_headers,
            json={
                "client_reference": str(uuid4()),
                "completion_submission_id": str(uuid4()),
            },
        )
    ).status_code == 409
    monkeypatch.setattr(
        completion_router,
        "revoke_completion_request",
        AsyncMock(side_effect=completion_service.CompletionResourceNotFoundError(job_id)),
    )
    assert (
        await client.post(
            f"{request_url}/{uuid4()}/revoke",
            headers=manager_headers,
            json={"reason": "없는 요청"},
        )
    ).status_code == 404

    no_retention_app = create_app(Settings(environment=AppEnvironment.TEST))
    no_retention_app.state.database_session_factory = factory
    no_retention_app.state.storage_port = storage
    async with AsyncClient(
        transport=ASGITransport(app=no_retention_app),
        base_url="http://testserver",
    ) as no_retention_client:
        unavailable = await no_retention_client.post(
            f"{request_url}/{uuid4()}/decision",
            headers=customer_headers,
            json={"decision": "confirm"},
        )
    assert unavailable.status_code == 503
