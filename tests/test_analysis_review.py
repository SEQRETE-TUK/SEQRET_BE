"""A-06 customer-facing AI draft review API tests."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import AppEnvironment, Settings
from app.contracts.ai import AnalysisResult, DraftItem
from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.primitives import AnalysisRunId, CaptureSessionId, MediaAssetId
from app.main import create_app
from app.modules.analysis_review.schemas import AnalysisReviewComplete
from app.modules.analysis_review.service import (
    AnalysisReviewConflictError,
    _aware,
    get_analysis_review,
)
from app.modules.analysis_workflow.models import (
    CaptureAnalysisDispatch,
    CaptureAnalysisStatus,
)
from app.modules.capture.models import CaptureSession, MediaAsset
from app.modules.scope.models import ScopeVersion
from app.modules.scope.service import import_analysis_draft
from app.platform.db import Base, create_session_factory

NOW = datetime(2026, 8, 15, 11, 0, tzinfo=UTC)


@dataclass(frozen=True)
class ReviewHarness:
    client: AsyncClient
    factory: async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class ReviewSeed:
    job_id: UUID
    participant_id: UUID
    capture_session_id: UUID
    analysis_run_id: UUID
    source_scope_version_id: UUID
    zone_ids: tuple[UUID, UUID]


@pytest.fixture
async def review_harness(tmp_path: Path) -> AsyncIterator[ReviewHarness]:
    database_path = (tmp_path / "analysis-review.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
    factory = create_session_factory(engine)
    application = create_app(Settings(environment=AppEnvironment.TEST))
    application.state.database_session_factory = factory
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield ReviewHarness(client=client, factory=factory)
    await engine.dispose()


async def _create_job(harness: ReviewHarness, title: str = "AI 검토 테스트") -> dict[str, Any]:
    response = await harness.client.post(
        "/api/v1/move-jobs",
        json={
            "title": title,
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


def _participant_id(created: dict[str, Any], role: str) -> UUID:
    return UUID(
        next(
            participant["id"]
            for participant in created["job"]["participants"]
            if participant["role"] == role
        )
    )


async def _seed_completed_analysis(
    harness: ReviewHarness,
    created: dict[str, Any],
) -> ReviewSeed:
    job_id = UUID(created["job"]["id"])
    participant_id = _participant_id(created, "customer")
    zones = created["job"]["locations"][0]["room_zones"]
    zone_ids = (UUID(zones[0]["id"]), UUID(zones[1]["id"]))
    capture_session_id = uuid4()
    analysis_run_id = uuid4()
    ready_ids = (uuid4(), uuid4())
    failed_id = uuid4()
    result = AnalysisResult(
        analysis_run_id=AnalysisRunId(analysis_run_id),
        capture_session_id=CaptureSessionId(capture_session_id),
        model_name="private-model",
        model_version="private-version",
        prompt_version="private-prompt",
        draft_items=(
            DraftItem(
                item_key="box",
                description="이삿짐 상자 4개",
                confidence=0.91,
                source_media_asset_ids=(MediaAssetId(ready_ids[0]),),
            ),
        ),
        review_required_items=(
            DraftItem(
                item_key="wardrobe",
                description="확인이 필요한 옷장",
                confidence=0.42,
                source_media_asset_ids=(MediaAssetId(ready_ids[1]),),
            ),
        ),
    )
    async with harness.factory.begin() as session:
        session.add(
            CaptureSession(
                id=capture_session_id,
                job_id=job_id,
                created_by_participant_id=participant_id,
                created_at=NOW,
            )
        )
        session.add_all(
            [
                MediaAsset(
                    id=ready_ids[0],
                    capture_session_id=capture_session_id,
                    room_zone_id=zone_ids[0],
                    media_purpose=MediaPurpose.INVENTORY,
                    status=MediaAssetStatus.READY,
                    object_key=f"jobs/{job_id}/captures/{capture_session_id}/ready-0",
                    content_type="image/jpeg",
                    expected_size_bytes=10,
                    actual_size_bytes=10,
                    sha256_hex="a" * 64,
                    generation="1",
                    created_at=NOW,
                    uploaded_at=NOW,
                ),
                MediaAsset(
                    id=ready_ids[1],
                    capture_session_id=capture_session_id,
                    room_zone_id=zone_ids[1],
                    media_purpose=MediaPurpose.INVENTORY,
                    status=MediaAssetStatus.READY,
                    object_key=f"jobs/{job_id}/captures/{capture_session_id}/ready-1",
                    content_type="image/png",
                    expected_size_bytes=20,
                    actual_size_bytes=20,
                    sha256_hex="b" * 64,
                    generation="2",
                    created_at=NOW + timedelta(seconds=1),
                    uploaded_at=NOW,
                ),
                MediaAsset(
                    id=failed_id,
                    capture_session_id=capture_session_id,
                    room_zone_id=zone_ids[0],
                    media_purpose=MediaPurpose.INVENTORY,
                    status=MediaAssetStatus.FAILED,
                    object_key=f"jobs/{job_id}/captures/{capture_session_id}/failed",
                    content_type="image/jpeg",
                    expected_size_bytes=30,
                    actual_size_bytes=30,
                    sha256_hex=None,
                    generation="3",
                    created_at=NOW + timedelta(seconds=2),
                    uploaded_at=NOW,
                ),
            ]
        )
        await session.flush()
        imported = await import_analysis_draft(session, job_id, result)
        session.add(
            CaptureAnalysisDispatch(
                analysis_run_id=analysis_run_id,
                capture_session_id=capture_session_id,
                move_job_id=job_id,
                submitted_by_participant_id=participant_id,
                status=CaptureAnalysisStatus.COMPLETED,
                trace_id="0" * 32,
                scheduled_at=NOW,
                dispatch_attempt_count=1,
                provider_task_id="provider-task",
                last_attempt_at=NOW,
                scope_version_id=imported.id,
                submitted_at=NOW,
                completed_at=NOW,
            )
        )
    return ReviewSeed(
        job_id=job_id,
        participant_id=participant_id,
        capture_session_id=capture_session_id,
        analysis_run_id=analysis_run_id,
        source_scope_version_id=imported.id,
        zone_ids=zone_ids,
    )


def _complete_payload(seed: ReviewSeed) -> dict[str, Any]:
    return {
        "source_scope_version_id": str(seed.source_scope_version_id),
        "items": [
            {
                "item_key": "wardrobe",
                "room_zone_id": str(seed.zone_ids[1]),
                "description": "큰 옷장 1개",
            },
            {
                "item_key": "box",
                "room_zone_id": str(seed.zone_ids[0]),
                "description": "이삿짐 상자 5개",
            },
            {
                "item_key": "customer-lamp",
                "room_zone_id": str(seed.zone_ids[0]),
                "description": "고객이 추가한 스탠드",
            },
        ],
    }


@pytest.mark.anyio
async def test_review_query_is_customer_scoped_and_provider_neutral(
    review_harness: ReviewHarness,
) -> None:
    created = await _create_job(review_harness)
    other = await _create_job(review_harness, "다른 작업")
    seed = await _seed_completed_analysis(review_harness, created)
    url = f"/api/v1/move-jobs/{seed.job_id}/analysis-review"

    assert (await review_harness.client.get(url)).status_code == 401
    assert (
        await review_harness.client.get(
            url,
            headers=_headers(_secret(created, "company_manager")),
        )
    ).status_code == 403
    assert (
        await review_harness.client.get(
            f"/api/v1/move-jobs/{other['job']['id']}/analysis-review",
            headers=_headers(_secret(created, "customer")),
        )
    ).status_code == 404

    response = await review_harness.client.get(
        url,
        headers=_headers(_secret(created, "customer")),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["analysis_run_id"] == str(seed.analysis_run_id)
    assert body["source_scope_version_id"] == str(seed.source_scope_version_id)
    assert body["review_scope_version_id"] is None
    assert body["review_completed_at"] is None
    assert body["zones"] == [
        {
            "room_zone_id": str(seed.zone_ids[0]),
            "name": "거실",
            "sort_order": 0,
            "total_media_count": 2,
            "ready_media_count": 1,
            "failed_media_count": 1,
        },
        {
            "room_zone_id": str(seed.zone_ids[1]),
            "name": "안방",
            "sort_order": 1,
            "total_media_count": 1,
            "ready_media_count": 1,
            "failed_media_count": 0,
        },
    ]
    assert [item["item_key"] for item in body["items"]] == ["box", "wardrobe"]
    assert body["items"][0]["source"] == "ai"
    assert body["items"][0]["confidence"] == 0.91
    assert body["items"][0]["review_required"] is False
    assert body["items"][1]["review_required"] is True
    assert "private-model" not in response.text
    assert "private-version" not in response.text
    assert "private-prompt" not in response.text


@pytest.mark.anyio
async def test_review_complete_is_atomic_idempotent_and_detects_conflicts(
    review_harness: ReviewHarness,
) -> None:
    created = await _create_job(review_harness)
    seed = await _seed_completed_analysis(review_harness, created)
    url = f"/api/v1/move-jobs/{seed.job_id}/analysis-review/complete"
    headers = _headers(_secret(created, "customer"))
    payload = _complete_payload(seed)

    first = await review_harness.client.post(url, headers=headers, json=payload)
    assert first.status_code == 200
    body = first.json()
    review_scope_version_id = body["review_scope_version_id"]
    assert review_scope_version_id is not None
    assert body["review_completed_at"] is not None
    assert [item["item_key"] for item in body["items"]] == [
        "box",
        "customer-lamp",
        "wardrobe",
    ]
    added = next(item for item in body["items"] if item["item_key"] == "customer-lamp")
    assert added == {
        "item_key": "customer-lamp",
        "room_zone_id": str(seed.zone_ids[0]),
        "description": "고객이 추가한 스탠드",
        "source": "customer",
        "confidence": None,
        "review_required": False,
        "source_media_asset_ids": [],
    }

    replay_payload = {**payload, "items": list(reversed(payload["items"]))}
    replay = await review_harness.client.post(url, headers=headers, json=replay_payload)
    assert replay.status_code == 200
    assert replay.json()["review_scope_version_id"] == review_scope_version_id

    loaded = await review_harness.client.get(url.removesuffix("/complete"), headers=headers)
    assert loaded.status_code == 200
    assert loaded.json() == replay.json()

    conflicting_payload = _complete_payload(seed)
    conflicting_payload["items"][0]["description"] = "다른 옷장 설명"
    conflict = await review_harness.client.post(
        url,
        headers=headers,
        json=conflicting_payload,
    )
    assert conflict.status_code == 409
    async with review_harness.factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(ScopeVersion).where(ScopeVersion.job_id == seed.job_id)
        )
    assert count == 2


@pytest.mark.anyio
async def test_review_rejects_not_ready_stale_and_invalid_commands(
    review_harness: ReviewHarness,
) -> None:
    empty_job = await _create_job(review_harness, "분석 없음")
    empty_url = f"/api/v1/move-jobs/{empty_job['job']['id']}/analysis-review"
    empty_headers = _headers(_secret(empty_job, "customer"))
    assert (await review_harness.client.get(empty_url, headers=empty_headers)).status_code == 404

    created = await _create_job(review_harness)
    seed = await _seed_completed_analysis(review_harness, created)
    headers = _headers(_secret(created, "customer"))
    url = f"/api/v1/move-jobs/{seed.job_id}/analysis-review/complete"
    payload = _complete_payload(seed)

    stale_source = {**payload, "source_scope_version_id": str(uuid4())}
    assert (
        await review_harness.client.post(url, headers=headers, json=stale_source)
    ).status_code == 409

    duplicate = _complete_payload(seed)
    duplicate["items"].append(duplicate["items"][0])
    assert (
        await review_harness.client.post(url, headers=headers, json=duplicate)
    ).status_code == 422

    other = await _create_job(review_harness, "외부 구역")
    foreign_zone = _complete_payload(seed)
    foreign_zone["items"][0]["room_zone_id"] = other["job"]["locations"][0]["room_zones"][0]["id"]
    assert (
        await review_harness.client.post(url, headers=headers, json=foreign_zone)
    ).status_code == 404

    newer_capture_id = uuid4()
    async with review_harness.factory.begin() as session:
        session.add(
            CaptureSession(
                id=newer_capture_id,
                job_id=seed.job_id,
                created_by_participant_id=seed.participant_id,
                created_at=NOW + timedelta(hours=1),
            )
        )
        session.add(
            CaptureAnalysisDispatch(
                analysis_run_id=uuid4(),
                capture_session_id=newer_capture_id,
                move_job_id=seed.job_id,
                submitted_by_participant_id=seed.participant_id,
                status=CaptureAnalysisStatus.RUNNING,
                trace_id="1" * 32,
                scheduled_at=NOW + timedelta(hours=1),
                dispatch_attempt_count=1,
                provider_task_id="newer-task",
                last_attempt_at=NOW + timedelta(hours=1),
                submitted_at=NOW + timedelta(hours=1),
            )
        )
    assert (
        await review_harness.client.get(url.removesuffix("/complete"), headers=headers)
    ).status_code == 409


@pytest.mark.anyio
async def test_review_rejects_existing_foreign_child_and_locked_source(
    review_harness: ReviewHarness,
) -> None:
    created = await _create_job(review_harness)
    seed = await _seed_completed_analysis(review_harness, created)
    versions_url = f"/api/v1/move-jobs/{seed.job_id}/scope-versions"
    payload = _complete_payload(seed)
    low_level_payload = {
        "parent_version_id": str(seed.source_scope_version_id),
        "content": {"schema_version": 1, "items": payload["items"]},
    }
    manager_child = await review_harness.client.post(
        versions_url,
        headers=_headers(_secret(created, "company_manager")),
        json=low_level_payload,
    )
    assert manager_child.status_code == 201
    review_url = f"/api/v1/move-jobs/{seed.job_id}/analysis-review/complete"
    assert (
        await review_harness.client.get(
            review_url.removesuffix("/complete"),
            headers=_headers(_secret(created, "customer")),
        )
    ).status_code == 409
    assert (
        await review_harness.client.post(
            review_url,
            headers=_headers(_secret(created, "customer")),
            json=payload,
        )
    ).status_code == 409

    locked_created = await _create_job(review_harness, "잠긴 원본")
    locked_seed = await _seed_completed_analysis(review_harness, locked_created)
    async with review_harness.factory.begin() as session:
        source = await session.get(ScopeVersion, locked_seed.source_scope_version_id)
        assert source is not None
        source.locked_at = NOW
    assert (
        await review_harness.client.post(
            f"/api/v1/move-jobs/{locked_seed.job_id}/analysis-review/complete",
            headers=_headers(_secret(locked_created, "customer")),
            json=_complete_payload(locked_seed),
        )
    ).status_code == 409


@pytest.mark.anyio
async def test_review_rejects_corrupt_internal_links_without_exposing_them(
    review_harness: ReviewHarness,
) -> None:
    created = await _create_job(review_harness)
    seed = await _seed_completed_analysis(review_harness, created)

    async with review_harness.factory() as session:
        row = await session.get(CaptureAnalysisDispatch, seed.analysis_run_id)
        source = await session.get(ScopeVersion, seed.source_scope_version_id)
        assert row is not None and source is not None
        original_analysis_source = source.analysis_source
        assert original_analysis_source is not None
        row.scope_version_id = uuid4()
        with session.no_autoflush, pytest.raises(AnalysisReviewConflictError):
            await get_analysis_review(session, seed.job_id, seed.participant_id)
        row.scope_version_id = seed.source_scope_version_id
        source.analysis_source = None
        with session.no_autoflush, pytest.raises(AnalysisReviewConflictError):
            await get_analysis_review(session, seed.job_id, seed.participant_id)
        source.analysis_source = original_analysis_source
        row.completed_at = None
        with session.no_autoflush, pytest.raises(AnalysisReviewConflictError):
            await get_analysis_review(session, seed.job_id, seed.participant_id)

    assert _aware(NOW) is NOW
    naive = NOW.replace(tzinfo=None)
    assert _aware(naive).tzinfo is UTC


def test_review_schema_rejects_duplicate_keys() -> None:
    zone_id = uuid4()
    item = {
        "item_key": "box",
        "room_zone_id": str(zone_id),
        "description": "상자",
    }
    with pytest.raises(ValueError, match="analysis review item keys must be unique"):
        AnalysisReviewComplete.model_validate(
            {
                "source_scope_version_id": str(uuid4()),
                "items": [item, item],
            }
        )
