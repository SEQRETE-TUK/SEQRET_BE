"""A-06 customer-facing AI draft review API tests."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from httpx2 import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import AppEnvironment, Settings
from app.contracts.ai import (
    AnalysisCarryDistanceCondition,
    AnalysisFloorCondition,
    AnalysisResult,
    DraftItem,
    DraftLocationCondition,
)
from app.contracts.fakes import FakeObjectStorage
from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.ports import ProviderError, ProviderErrorKind
from app.contracts.primitives import AnalysisRunId, CaptureSessionId, MediaAssetId
from app.main import create_app
from app.modules.analysis_review.schemas import AnalysisReviewComplete
from app.modules.analysis_review.service import (
    AnalysisReviewConflictError,
    _aware,
    _current_items,
    get_analysis_review,
)
from app.modules.analysis_workflow.models import (
    CaptureAnalysisDispatch,
    CaptureAnalysisStatus,
)
from app.modules.capture.models import CaptureSession, MediaAsset
from app.modules.scope.models import ScopeVersion
from app.modules.scope.schemas import (
    ScopeContent,
    ScopeItemReviewStatus,
    ScopeItemSource,
    ScopeItemV2,
)
from app.modules.scope.service import import_analysis_draft
from app.platform.db import Base, create_session_factory

NOW = datetime(2026, 8, 15, 11, 0, tzinfo=UTC)


@dataclass(frozen=True)
class ReviewHarness:
    client: AsyncClient
    factory: async_sessionmaker[AsyncSession]
    storage: FakeObjectStorage


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
    storage = FakeObjectStorage()
    application.state.storage_port = storage
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield ReviewHarness(client=client, factory=factory, storage=storage)
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
    *,
    structured: bool = False,
) -> ReviewSeed:
    job_id = UUID(created["job"]["id"])
    participant_id = _participant_id(created, "customer")
    zones = created["job"]["locations"][0]["room_zones"]
    zone_ids = (UUID(zones[0]["id"]), UUID(zones[1]["id"]))
    capture_session_id = uuid4()
    analysis_run_id = uuid4()
    ready_ids = (uuid4(), uuid4())
    failed_id = uuid4()
    if structured:
        location_id = UUID(created["job"]["locations"][0]["id"])
        result = AnalysisResult(
            analysis_run_id=AnalysisRunId(analysis_run_id),
            capture_session_id=CaptureSessionId(capture_session_id),
            model_name="private-model",
            model_version="private-version",
            prompt_version="private-prompt-v2",
            result_schema_version=2,
            draft_items=(
                DraftItem(
                    item_key="box",
                    description="이삿짐 상자 4개",
                    name="이삿짐 상자",
                    quantity=4,
                    unit="개",
                    work_note="완충 포장",
                    confidence=0.91,
                    source_media_asset_ids=(MediaAssetId(ready_ids[0]),),
                ),
            ),
            review_required_items=(
                DraftItem(
                    item_key="wardrobe",
                    description="수량 확인이 필요한 옷장",
                    name="옷장",
                    confidence=0.42,
                    source_media_asset_ids=(MediaAssetId(ready_ids[1]),),
                ),
            ),
            location_condition_suggestions=(
                DraftLocationCondition(
                    location_id=location_id,
                    location_kind="origin",
                    residence_type="studio",
                    floor=AnalysisFloorCondition(status="known", value=3),
                    elevator="available",
                    stairs="not_required",
                    parking_access="restricted",
                    carry_distance=AnalysisCarryDistanceCondition(
                        status="known",
                        value_m=35,
                    ),
                    access_note="골목 진입 확인 필요",
                    confidence=0.74,
                    review_required_fields=("parking_access",),
                    source_media_asset_ids=(MediaAssetId(ready_ids[1]),),
                ),
            ),
        )
    else:
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


async def _seed_ready_video(
    harness: ReviewHarness,
    seed: ReviewSeed,
) -> tuple[UUID, str]:
    media_asset_id = uuid4()
    object_key = (
        f"jobs/{seed.job_id}/captures/{seed.capture_session_id}/analysis-video.mp4"
    )
    async with harness.factory.begin() as session:
        session.add(
            MediaAsset(
                id=media_asset_id,
                capture_session_id=seed.capture_session_id,
                room_zone_id=seed.zone_ids[0],
                media_purpose=MediaPurpose.INVENTORY,
                status=MediaAssetStatus.READY,
                object_key=object_key,
                content_type="video/mp4",
                expected_size_bytes=100,
                actual_size_bytes=100,
                sha256_hex="c" * 64,
                generation="4",
                created_at=NOW + timedelta(seconds=3),
                uploaded_at=NOW,
            )
        )
    return media_asset_id, object_key


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


def test_analysis_review_v2_shape_validation() -> None:
    base = {"item_key": "box", "room_zone_id": str(uuid4())}
    for invalid in (base, {**base, "description": "박스", "name": "박스"}):
        with pytest.raises(ValidationError, match="exactly one schema shape"):
            AnalysisReviewComplete.model_validate(
                {"source_scope_version_id": str(uuid4()), "items": [invalid]}
            )
    with pytest.raises(ValidationError, match="require name, quantity, and unit"):
        AnalysisReviewComplete.model_validate(
            {
                "source_scope_version_id": str(uuid4()),
                "scope_schema_version": 2,
                "items": [{**base, "name": "박스"}],
            }
        )
    with pytest.raises(ValidationError, match="legacy review items cannot include work note"):
        AnalysisReviewComplete.model_validate(
            {
                "source_scope_version_id": str(uuid4()),
                "items": [{**base, "description": "박스", "work_note": "포장"}],
            }
        )
    with pytest.raises(ValidationError, match="shape must match scope schema version"):
        AnalysisReviewComplete.model_validate(
            {
                "source_scope_version_id": str(uuid4()),
                "scope_schema_version": 2,
                "items": [{**base, "description": "박스"}],
            }
        )
    conditions = {
        "residence_type": "unknown",
        "floor": {"status": "unknown", "value": None},
        "elevator": "unknown",
        "stairs": "unknown",
        "parking_access": "unknown",
        "carry_distance": {"status": "unknown", "value_m": None},
        "access_note": None,
    }
    location_id = uuid4()
    location = {
        "location_id": str(location_id),
        "kind": "origin",
        "conditions": conditions,
    }
    with pytest.raises(ValidationError, match="v1 cannot contain location conditions"):
        AnalysisReviewComplete.model_validate(
            {
                "source_scope_version_id": str(uuid4()),
                "items": [{**base, "description": "박스"}],
                "location_conditions": [location],
            }
        )
    structured_item = {**base, "name": "박스", "quantity": 1, "unit": "개"}
    with pytest.raises(ValidationError, match="location IDs must be unique"):
        AnalysisReviewComplete.model_validate(
            {
                "source_scope_version_id": str(uuid4()),
                "scope_schema_version": 2,
                "items": [structured_item],
                "location_conditions": [
                    location,
                    {**location, "kind": "destination"},
                ],
            }
        )
    with pytest.raises(ValidationError, match="location kinds must be unique"):
        AnalysisReviewComplete.model_validate(
            {
                "source_scope_version_id": str(uuid4()),
                "scope_schema_version": 2,
                "items": [structured_item],
                "location_conditions": [
                    location,
                    {**location, "location_id": str(uuid4())},
                ],
            }
        )


def test_analysis_review_maps_unconfirmed_ai_v2_item() -> None:
    capture_session_id = CaptureSessionId(uuid4())
    media_asset_id = MediaAssetId(uuid4())
    result = AnalysisResult(
        analysis_run_id=AnalysisRunId(uuid4()),
        capture_session_id=capture_session_id,
        model_name="fake",
        model_version="v1",
        prompt_version="inventory-v2",
        draft_items=(),
        review_required_items=(
            DraftItem(
                item_key="box",
                description="수량 확인 필요 박스",
                confidence=0.4,
                source_media_asset_ids=(media_asset_id,),
            ),
        ),
    )
    content = ScopeContent(
        schema_version=2,
        items=(
            ScopeItemV2(
                item_key="box",
                room_zone_id=uuid4(),
                name="박스",
                review_status=ScopeItemReviewStatus.REVIEW_REQUIRED,
                source=ScopeItemSource.AI,
            ),
        ),
    )
    item = _current_items(result, content)[0]
    assert item.source == "ai"
    assert item.scope_source is ScopeItemSource.AI
    assert item.review_required is True


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
    assert body["video_preview"] is None
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
async def test_review_returns_signed_video_preview(
    review_harness: ReviewHarness,
) -> None:
    created = await _create_job(review_harness)
    seed = await _seed_completed_analysis(review_harness, created)
    media_asset_id, object_key = await _seed_ready_video(review_harness, seed)

    response = await review_harness.client.get(
        f"/api/v1/move-jobs/{seed.job_id}/analysis-review",
        headers=_headers(_secret(created, "customer")),
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    preview = response.json()["video_preview"]
    assert preview["media_asset_id"] == str(media_asset_id)
    assert preview["content_type"] == "video/mp4"
    assert preview["read_url"] == (
        f"https://storage.invalid/read/{object_key}?generation=4"
    )
    assert datetime.fromisoformat(preview["expires_at"]) > datetime.now(UTC)


@pytest.mark.anyio
async def test_review_maps_video_preview_storage_failure(
    review_harness: ReviewHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = await _create_job(review_harness)
    seed = await _seed_completed_analysis(review_harness, created)
    await _seed_ready_video(review_harness, seed)

    async def fail_read_url(**_: Any) -> str:
        raise ProviderError(
            ProviderErrorKind.UNAVAILABLE,
            "offline",
            retryable=True,
        )

    monkeypatch.setattr(review_harness.storage, "create_read_url", fail_read_url)
    response = await review_harness.client.get(
        f"/api/v1/move-jobs/{seed.job_id}/analysis-review",
        headers=_headers(_secret(created, "customer")),
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "storage is unavailable"}


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
        "name": "고객이 추가한 스탠드",
        "quantity": None,
        "unit": None,
        "work_note": None,
        "review_status": "confirmed",
        "scope_source": "customer",
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
async def test_review_completion_creates_structured_v2_scope(
    review_harness: ReviewHarness,
) -> None:
    created = await _create_job(review_harness)
    seed = await _seed_completed_analysis(review_harness, created)
    response = await review_harness.client.post(
        f"/api/v1/move-jobs/{seed.job_id}/analysis-review/complete",
        headers=_headers(_secret(created, "customer")),
        json={
            "source_scope_version_id": str(seed.source_scope_version_id),
            "scope_schema_version": 2,
            "items": [
                {
                    "item_key": "wardrobe",
                    "room_zone_id": str(seed.zone_ids[1]),
                    "name": "큰 옷장",
                    "quantity": 1,
                    "unit": "개",
                    "work_note": "분해 후 재조립",
                }
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["scope_schema_version"] == 2
    assert body["items"][0]["quantity"] == 1
    assert body["items"][0]["unit"] == "개"
    assert body["items"][0]["scope_source"] == "customer"
    assert body["location_conditions"][0]["conditions"]["residence_type"] == "unknown"


@pytest.mark.anyio
async def test_review_v2_exposes_and_persists_customer_edited_location_conditions(
    review_harness: ReviewHarness,
) -> None:
    created = await _create_job(review_harness)
    seed = await _seed_completed_analysis(review_harness, created, structured=True)
    headers = _headers(_secret(created, "customer"))
    url = f"/api/v1/move-jobs/{seed.job_id}/analysis-review"

    loaded = await review_harness.client.get(url, headers=headers)
    assert loaded.status_code == 200
    source = loaded.json()
    assert source["scope_schema_version"] == 2
    assert source["items"][0]["name"] == "이삿짐 상자"
    assert source["items"][0]["quantity"] == 4
    assert source["items"][1]["review_required"] is True
    suggestion = source["location_condition_suggestions"][0]
    assert suggestion["confidence"] == 0.74
    assert suggestion["review_required_fields"] == ["parking_access"]
    assert suggestion["source_media_asset_ids"]
    assert source["location_conditions"][0]["conditions"]["residence_type"] == "studio"

    location_id = created["job"]["locations"][0]["id"]
    payload: dict[str, Any] = {
        "source_scope_version_id": str(seed.source_scope_version_id),
        "scope_schema_version": 2,
        "items": [
            {
                "item_key": "box",
                "room_zone_id": str(seed.zone_ids[0]),
                "name": "이삿짐 상자",
                "quantity": 5,
                "unit": "개",
                "work_note": "완충 포장",
            },
            {
                "item_key": "wardrobe",
                "room_zone_id": str(seed.zone_ids[1]),
                "name": "옷장",
                "quantity": 1,
                "unit": "개",
                "work_note": "분해 후 재조립",
            },
        ],
        "location_conditions": [
            {
                "location_id": location_id,
                "kind": "origin",
                "conditions": {
                    "residence_type": "studio",
                    "floor": {"status": "known", "value": 3},
                    "elevator": "available",
                    "stairs": "not_required",
                    "parking_access": "available",
                    "carry_distance": {"status": "known", "value_m": 20},
                    "access_note": "고객이 주차 가능 확인",
                },
            }
        ],
    }
    completed = await review_harness.client.post(f"{url}/complete", headers=headers, json=payload)
    assert completed.status_code == 200
    body = completed.json()
    assert body["review_scope_version_id"] is not None
    assert body["items"][0]["quantity"] == 5
    assert body["items"][1]["review_status"] == "confirmed"
    assert body["location_conditions"][0]["conditions"]["parking_access"] == "available"
    assert body["location_conditions"][0]["conditions"]["carry_distance"]["value_m"] == 20
    assert body["location_condition_suggestions"] == source["location_condition_suggestions"]

    replay = await review_harness.client.post(
        f"{url}/complete",
        headers=headers,
        json={**payload, "items": list(reversed(payload["items"]))},
    )
    assert replay.status_code == 200
    assert replay.json()["review_scope_version_id"] == body["review_scope_version_id"]


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

    foreign_location = {
        "source_scope_version_id": str(seed.source_scope_version_id),
        "scope_schema_version": 2,
        "items": [
            {
                "item_key": "box",
                "room_zone_id": str(seed.zone_ids[0]),
                "name": "박스",
                "quantity": 1,
                "unit": "개",
            }
        ],
        "location_conditions": [
            {
                "location_id": str(uuid4()),
                "kind": "origin",
                "conditions": {
                    "residence_type": "unknown",
                    "floor": {"status": "unknown", "value": None},
                    "elevator": "unknown",
                    "stairs": "unknown",
                    "parking_access": "unknown",
                    "carry_distance": {"status": "unknown", "value_m": None},
                    "access_note": None,
                },
            }
        ],
    }
    assert (
        await review_harness.client.post(url, headers=headers, json=foreign_location)
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
