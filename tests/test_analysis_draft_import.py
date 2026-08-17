"""AI analysis-result import and provenance boundary tests."""

import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.contracts.actor import ParticipantRole
from app.contracts.ai import (
    AnalysisCarryDistanceCondition,
    AnalysisFloorCondition,
    AnalysisResult,
    DraftItem,
    DraftLocationCondition,
)
from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.primitives import AnalysisRunId, CaptureSessionId, MediaAssetId
from app.modules.capture.models import CaptureSession, MediaAsset
from app.modules.move_job.models import Location, LocationKind, RoomZone
from app.modules.move_job.schemas import (
    LocationCreate,
    MoveJobCreate,
    ParticipantCreate,
    RoomZoneCreate,
)
from app.modules.move_job.service import create_move_job
from app.modules.scope.models import ScopeVersion
from app.modules.scope.schemas import (
    ScopeContent,
    ScopeItem,
    ScopeItemReviewStatus,
    ScopeItemSource,
    ScopeItemV2,
    ScopeVersionCreate,
)
from app.modules.scope.service import (
    AnalysisDraftInvalidError,
    ScopeApprovalConflictError,
    ScopeResourceNotFoundError,
    ScopeVersionConflictError,
    approve_scope_version,
    create_scope_version,
    import_analysis_draft,
    list_scope_versions,
)
from app.platform.db import Base, create_session_factory

AnalysisDatabase = async_sessionmaker[AsyncSession]
AnalysisSeed = tuple[UUID, UUID, UUID, UUID, UUID, UUID, UUID]


@pytest.fixture
async def analysis_database(tmp_path: Path) -> AsyncIterator[AnalysisDatabase]:
    database_path = (tmp_path / "analysis.sqlite3").as_posix()
    sync_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
    factory = create_session_factory(engine)
    yield factory
    await engine.dispose()


async def _seed_analysis(factory: AnalysisDatabase) -> AnalysisSeed:
    async with factory.begin() as session:
        created = await create_move_job(
            session,
            MoveJobCreate(
                title="AI 초안 가져오기",
                participants=(
                    ParticipantCreate(role=ParticipantRole.CUSTOMER, display_name="고객"),
                    ParticipantCreate(
                        role=ParticipantRole.COMPANY_MANAGER,
                        display_name="관리자",
                    ),
                    ParticipantCreate(
                        role=ParticipantRole.FIELD_WORKER,
                        display_name="현장 담당",
                    ),
                ),
                locations=(
                    LocationCreate(
                        kind=LocationKind.ORIGIN,
                        label="출발지",
                        room_zones=(
                            RoomZoneCreate(name="거실", sort_order=0),
                            RoomZoneCreate(name="안방", sort_order=1),
                        ),
                    ),
                ),
            ),
        )
        customer_id = next(
            participant.id
            for participant in created.job.participants
            if participant.role is ParticipantRole.CUSTOMER
        )
        zone_a, zone_b = created.job.locations[0].room_zones
        capture = CaptureSession(
            job_id=created.job.id,
            created_by_participant_id=customer_id,
        )
        session.add(capture)
        await session.flush()
        asset_a = MediaAsset(
            capture_session_id=capture.id,
            room_zone_id=zone_a.id,
            media_purpose=MediaPurpose.INVENTORY,
            status=MediaAssetStatus.UPLOADED,
            object_key=f"analysis/{uuid4()}",
            content_type="image/jpeg",
            expected_size_bytes=10,
            actual_size_bytes=10,
            generation="1",
            uploaded_at=datetime.now(UTC),
        )
        asset_b = MediaAsset(
            capture_session_id=capture.id,
            room_zone_id=zone_b.id,
            media_purpose=MediaPurpose.CONDITION,
            status=MediaAssetStatus.READY,
            object_key=f"analysis/{uuid4()}",
            content_type="image/jpeg",
            expected_size_bytes=10,
            actual_size_bytes=10,
            generation="2",
            uploaded_at=datetime.now(UTC),
        )
        session.add_all((asset_a, asset_b))
        await session.flush()
        return (
            created.job.id,
            customer_id,
            capture.id,
            zone_a.id,
            zone_b.id,
            asset_a.id,
            asset_b.id,
        )


def _analysis_result(
    capture_session_id: UUID,
    asset_a_id: UUID,
    asset_b_id: UUID,
) -> AnalysisResult:
    return AnalysisResult(
        analysis_run_id=AnalysisRunId(uuid4()),
        capture_session_id=CaptureSessionId(capture_session_id),
        model_name="gemini",
        model_version="2.5",
        prompt_version="scope-v1",
        draft_items=(
            DraftItem(
                item_key="sofa",
                description="소파 이동",
                confidence=0.91,
                source_media_asset_ids=(MediaAssetId(asset_a_id),),
            ),
        ),
        review_required_items=(
            DraftItem(
                item_key="bed",
                description="침대 분해 후 이동",
                confidence=0.45,
                source_media_asset_ids=(MediaAssetId(asset_b_id),),
            ),
        ),
    )


@pytest.mark.anyio
async def test_analysis_result_becomes_editable_unconfirmed_scope_with_provenance(
    analysis_database: AnalysisDatabase,
) -> None:
    job_id, customer_id, capture_id, zone_a_id, _, asset_a_id, asset_b_id = await _seed_analysis(
        analysis_database
    )
    result = _analysis_result(capture_id, asset_a_id, asset_b_id)

    async with analysis_database.begin() as session:
        imported = await import_analysis_draft(session, job_id, result)

        assert imported.sequence_number == 1
        assert imported.created_by_participant_id is None
        assert imported.analysis_source == result
        assert imported.approval_roles == ()
        assert imported.locked_at is None
        assert [item.item_key for item in imported.content.items] == ["bed", "sofa"]
        canonical = json.dumps(
            imported.content.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        assert imported.content_hash == hashlib.sha256(canonical.encode()).hexdigest()

        with pytest.raises(ScopeApprovalConflictError):
            await approve_scope_version(
                session,
                job_id,
                imported.id,
                customer_id,
                ParticipantRole.CUSTOMER,
            )

        edited = await create_scope_version(
            session,
            job_id,
            customer_id,
            ScopeVersionCreate(
                parent_version_id=imported.id,
                content=ScopeContent(
                    items=(
                        ScopeItem(
                            item_key="sofa",
                            room_zone_id=zone_a_id,
                            description="소파 포장 후 이동",
                        ),
                    )
                ),
            ),
        )
        assert edited.sequence_number == 2
        assert edited.created_by_participant_id == customer_id
        assert edited.analysis_source is None

    async with analysis_database.begin() as session:
        versions = await list_scope_versions(session, job_id)
        with pytest.raises(ScopeVersionConflictError):
            await import_analysis_draft(session, job_id, result)

    assert versions[0].analysis_source == result
    assert versions[1].analysis_source is None


@pytest.mark.anyio
async def test_analysis_import_rejects_invalid_capture_items_and_media(
    analysis_database: AnalysisDatabase,
) -> None:
    job_id, _, capture_id, _, _, asset_a_id, asset_b_id = await _seed_analysis(analysis_database)
    _, _, other_capture_id, _, _, other_asset_id, _ = await _seed_analysis(analysis_database)

    def result_with(*items: DraftItem, capture: UUID = capture_id) -> AnalysisResult:
        return AnalysisResult(
            analysis_run_id=AnalysisRunId(uuid4()),
            capture_session_id=CaptureSessionId(capture),
            model_name="gemini",
            model_version="2.5",
            prompt_version="scope-v1",
            draft_items=items,
        )

    valid_item = DraftItem(
        item_key="sofa",
        description="소파 이동",
        confidence=0.9,
        source_media_asset_ids=(MediaAssetId(asset_a_id),),
    )
    invalid_results = (
        result_with(),
        result_with(valid_item, valid_item),
        result_with(
            DraftItem(
                item_key="sofa",
                description="소파 이동",
                confidence=0.9,
            )
        ),
        result_with(
            DraftItem(
                item_key="sofa",
                description="소파 이동",
                confidence=0.9,
                source_media_asset_ids=(MediaAssetId(asset_a_id), MediaAssetId(asset_a_id)),
            )
        ),
        result_with(
            DraftItem(
                item_key="sofa",
                description="소파 이동",
                confidence=0.9,
                source_media_asset_ids=(MediaAssetId(uuid4()),),
            )
        ),
        result_with(
            DraftItem(
                item_key="mixed",
                description="구역이 섞인 제안",
                confidence=0.5,
                source_media_asset_ids=(
                    MediaAssetId(asset_a_id),
                    MediaAssetId(asset_b_id),
                ),
            )
        ),
        result_with(
            DraftItem(
                item_key="foreign",
                description="다른 촬영 미디어",
                confidence=0.5,
                source_media_asset_ids=(MediaAssetId(other_asset_id),),
            )
        ),
    )

    async with analysis_database.begin() as session:
        for result in invalid_results:
            with pytest.raises(AnalysisDraftInvalidError):
                await import_analysis_draft(session, job_id, result)

        with pytest.raises(ScopeResourceNotFoundError):
            await import_analysis_draft(
                session,
                job_id,
                result_with(valid_item, capture=other_capture_id),
            )

        pending = await session.get(MediaAsset, asset_a_id)
        assert pending is not None
        pending.status = MediaAssetStatus.PENDING_UPLOAD
        pending.actual_size_bytes = None
        pending.generation = None
        pending.uploaded_at = None
        await session.flush()
        with pytest.raises(AnalysisDraftInvalidError):
            await import_analysis_draft(session, job_id, result_with(valid_item))


def _structured_analysis_result(
    capture_id: UUID,
    item_asset_id: UUID,
    *,
    location_suggestions: tuple[DraftLocationCondition, ...] = (),
) -> AnalysisResult:
    return AnalysisResult(
        analysis_run_id=AnalysisRunId(uuid4()),
        capture_session_id=CaptureSessionId(capture_id),
        model_name="gemini",
        model_version="2.5",
        prompt_version="scope-v2",
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
                source_media_asset_ids=(MediaAssetId(item_asset_id),),
            ),
        ),
        review_required_items=(
            DraftItem(
                item_key="air-conditioner",
                description="에어컨 철거 여부 확인 필요",
                name="에어컨",
                confidence=0.42,
                source_media_asset_ids=(MediaAssetId(item_asset_id),),
            ),
        ),
        location_condition_suggestions=location_suggestions,
    )


def _location_suggestion(
    location_id: UUID,
    source_asset_id: UUID,
    *,
    location_kind: str = "origin",
) -> DraftLocationCondition:
    return DraftLocationCondition(
        location_id=location_id,
        location_kind=location_kind,  # type: ignore[arg-type]
        residence_type="studio",
        floor=AnalysisFloorCondition(status="known", value=3),
        elevator="available",
        stairs="not_required",
        parking_access="restricted",
        carry_distance=AnalysisCarryDistanceCondition(status="known", value_m=35),
        access_note="골목 진입 확인 필요",
        confidence=0.74,
        review_required_fields=("parking_access",),
        source_media_asset_ids=(MediaAssetId(source_asset_id),),
    )


@pytest.mark.anyio
async def test_analysis_v2_becomes_structured_scope_with_condition_suggestions(
    analysis_database: AnalysisDatabase,
) -> None:
    job_id, _, capture_id, zone_id, _, asset_id, condition_asset_id = await _seed_analysis(
        analysis_database
    )
    async with analysis_database() as session:
        zone = await session.get(RoomZone, zone_id)
        assert zone is not None
        location_id = zone.location_id
    result = _structured_analysis_result(
        capture_id,
        asset_id,
        location_suggestions=(_location_suggestion(location_id, condition_asset_id),),
    )

    async with analysis_database.begin() as session:
        imported = await import_analysis_draft(session, job_id, result)

    assert imported.content.schema_version == 2
    items = {item.item_key: item for item in imported.content.items}
    box = items["box"]
    pending = items["air-conditioner"]
    assert isinstance(box, ScopeItemV2)
    assert box.quantity == 4
    assert box.unit == "개"
    assert box.work_note == "완충 포장"
    assert box.review_status is ScopeItemReviewStatus.CONFIRMED
    assert box.source is ScopeItemSource.AI
    assert isinstance(pending, ScopeItemV2)
    assert pending.quantity is None
    assert pending.unit is None
    assert pending.review_status is ScopeItemReviewStatus.REVIEW_REQUIRED
    assert imported.content.location_conditions[0].location_id == location_id
    conditions = imported.content.location_conditions[0].conditions
    assert conditions.residence_type.value == "studio"
    assert conditions.floor.value == 3
    assert conditions.carry_distance.value_m == 35
    assert imported.analysis_source == result


@pytest.mark.anyio
async def test_analysis_v2_uses_current_location_snapshot_without_suggestion(
    analysis_database: AnalysisDatabase,
) -> None:
    job_id, _, capture_id, _, _, asset_id, _ = await _seed_analysis(analysis_database)

    async with analysis_database.begin() as session:
        imported = await import_analysis_draft(
            session,
            job_id,
            _structured_analysis_result(capture_id, asset_id),
        )

    assert imported.content.schema_version == 2
    assert imported.content.location_conditions[0].conditions.residence_type.value == "unknown"


@pytest.mark.anyio
async def test_analysis_v2_rejects_unknown_or_mismatched_location_suggestions(
    analysis_database: AnalysisDatabase,
) -> None:
    job_id, _, capture_id, zone_id, _, asset_id, condition_asset_id = await _seed_analysis(
        analysis_database
    )
    async with analysis_database() as session:
        zone = await session.get(RoomZone, zone_id)
        assert zone is not None
        location_id = zone.location_id
    invalid_suggestions = (
        _location_suggestion(uuid4(), condition_asset_id),
        _location_suggestion(location_id, condition_asset_id, location_kind="destination"),
    )

    async with analysis_database.begin() as session:
        for suggestion in invalid_suggestions:
            with pytest.raises(AnalysisDraftInvalidError):
                await import_analysis_draft(
                    session,
                    job_id,
                    _structured_analysis_result(
                        capture_id,
                        asset_id,
                        location_suggestions=(suggestion,),
                    ),
                )


@pytest.mark.anyio
async def test_analysis_v2_rejects_condition_evidence_from_another_location(
    analysis_database: AnalysisDatabase,
) -> None:
    job_id, _, capture_id, zone_id, _, asset_id, _ = await _seed_analysis(analysis_database)
    destination_id = uuid4()
    destination_zone_id = uuid4()
    destination_asset_id = uuid4()
    async with analysis_database.begin() as session:
        origin_zone = await session.get(RoomZone, zone_id)
        assert origin_zone is not None
        origin_location_id = origin_zone.location_id
        session.add(
            Location(
                id=destination_id,
                job_id=job_id,
                kind=LocationKind.DESTINATION,
                label="도착지",
            )
        )
        session.add(
            RoomZone(
                id=destination_zone_id,
                location_id=destination_id,
                name="현관",
                sort_order=0,
            )
        )
        session.add(
            MediaAsset(
                id=destination_asset_id,
                capture_session_id=capture_id,
                room_zone_id=destination_zone_id,
                media_purpose=MediaPurpose.CONDITION,
                status=MediaAssetStatus.READY,
                object_key=f"analysis/{uuid4()}",
                content_type="image/jpeg",
                expected_size_bytes=10,
                actual_size_bytes=10,
                generation="3",
                uploaded_at=datetime.now(UTC),
            )
        )
        await session.flush()
        with pytest.raises(AnalysisDraftInvalidError):
            await import_analysis_draft(
                session,
                job_id,
                _structured_analysis_result(
                    capture_id,
                    asset_id,
                    location_suggestions=(
                        _location_suggestion(origin_location_id, destination_asset_id),
                    ),
                ),
            )


@pytest.mark.anyio
async def test_analysis_v2_rejects_corrupt_missing_location_topology(
    analysis_database: AnalysisDatabase,
) -> None:
    job_id, _, capture_id, zone_id, _, asset_id, _ = await _seed_analysis(analysis_database)
    async with analysis_database.begin() as session:
        zone = await session.get(RoomZone, zone_id)
        assert zone is not None
        location = await session.get(Location, zone.location_id)
        assert location is not None
        location.job_id = uuid4()
        await session.flush()
        with pytest.raises(AnalysisDraftInvalidError):
            await import_analysis_draft(
                session,
                job_id,
                _structured_analysis_result(capture_id, asset_id),
            )


@pytest.mark.anyio
async def test_analysis_v2_rejects_corrupt_media_zone_topology(
    analysis_database: AnalysisDatabase,
) -> None:
    job_id, _, capture_id, _, _, asset_id, _ = await _seed_analysis(analysis_database)
    async with analysis_database.begin() as session:
        asset = await session.get(MediaAsset, asset_id)
        assert asset is not None
        asset.room_zone_id = uuid4()
        await session.flush()
        with pytest.raises(AnalysisDraftInvalidError):
            await import_analysis_draft(
                session,
                job_id,
                _structured_analysis_result(capture_id, asset_id),
            )


@pytest.mark.anyio
async def test_scope_version_origin_is_enforced_by_command_and_database(
    analysis_database: AnalysisDatabase,
) -> None:
    job_id, customer_id, capture_id, zone_id, _, asset_id, _ = await _seed_analysis(
        analysis_database
    )
    result = _analysis_result(capture_id, asset_id, asset_id)
    command = ScopeVersionCreate(
        content=ScopeContent(
            items=(
                ScopeItem(
                    item_key="sofa",
                    room_zone_id=zone_id,
                    description="소파 이동",
                ),
            )
        )
    )

    async with analysis_database() as session:
        with pytest.raises(AnalysisDraftInvalidError):
            await create_scope_version(session, job_id, None, command)
        with pytest.raises(AnalysisDraftInvalidError):
            await create_scope_version(
                session,
                job_id,
                customer_id,
                command,
                analysis_source=result,
            )

    with pytest.raises(IntegrityError):
        async with analysis_database.begin() as session:
            session.add(
                ScopeVersion(
                    job_id=job_id,
                    sequence_number=1,
                    content=command.content.model_dump(mode="json"),
                    content_hash="a" * 64,
                    created_by_participant_id=None,
                )
            )
            await session.flush()
