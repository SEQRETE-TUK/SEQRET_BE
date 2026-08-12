"""PostgreSQL integration tests for migrations and async connectivity."""

import os
import sys
from asyncio import SelectorEventLoop, gather
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from app.config import Settings
from app.contracts.actor import ParticipantRole
from app.contracts.ai import AnalysisResult, DraftItem
from app.contracts.fakes import FakeObjectStorage
from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.ports import StorageObjectMetadata
from app.contracts.primitives import AnalysisRunId, CaptureSessionId, MediaAssetId
from app.modules.capture.schemas import MediaUploadCreate
from app.modules.capture.service import (
    complete_media_upload,
    create_capture_session,
    create_media_upload,
)
from app.modules.move_job.models import LocationKind
from app.modules.move_job.schemas import (
    LocationCreate,
    MoveJobCreate,
    ParticipantCreate,
    RoomZoneCreate,
)
from app.modules.move_job.service import connect_participant, create_move_job, get_move_job
from app.modules.scope.models import ChangeRequestStatus
from app.modules.scope.schemas import (
    ChangeDecisionCreate,
    ChangeRequestCreate,
    ScopeContent,
    ScopeItem,
    ScopeVersionCreate,
)
from app.modules.scope.service import (
    ChangeRequestConflictError,
    ScopeApprovalConflictError,
    ScopeVersionConflictError,
    approve_scope_version,
    create_change_request,
    create_scope_version,
    decide_change_request,
    import_analysis_draft,
    list_change_requests,
    list_scope_versions,
)
from app.platform.db import create_database_engine, create_session_factory, transactional_session

ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_BASELINE = "fnd_a02_0001"
ALEMBIC_ANALYSIS_PREVIOUS = "a_05_0001"
ALEMBIC_PREVIOUS = "a_06_0001"
ALEMBIC_HEAD = "a_07_0001"
BUSINESS_TABLES = {
    "capture_session",
    "job_participant",
    "location",
    "media_asset",
    "move_job",
    "participant_access_token",
    "room_zone",
    "scope_version",
    "scope_approval",
    "change_request",
    "change_request_evidence",
}
TEST_DATABASE_ENV = "SEQRET_TEST_DATABASE_URL"
TEST_SCHEMA = "seqret_migration_test"


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, object]]:
    """Run psycopg on a selector loop when the test host is Windows."""

    options: dict[str, object] = {}
    if sys.platform == "win32":
        options["loop_factory"] = SelectorEventLoop
    return "asyncio", options


def _test_database_url() -> URL:
    raw_url = os.getenv(TEST_DATABASE_ENV)
    if raw_url is None:
        pytest.skip(f"{TEST_DATABASE_ENV} is not configured")

    url = make_url(raw_url)
    is_disposable_local_database = (
        url.drivername == "postgresql+psycopg"
        and url.host in {"127.0.0.1", "localhost"}
        and url.database == "seqret_test"
    )
    if not is_disposable_local_database:
        pytest.fail(f"{TEST_DATABASE_ENV} must target localhost database seqret_test")
    return url


def _alembic_config(url: URL) -> Config:
    configuration = Config(str(ROOT / "alembic.ini"))
    rendered_url = url.render_as_string(hide_password=False).replace("%", "%%")
    configuration.set_main_option("sqlalchemy.url", rendered_url)
    return configuration


def _current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()


@contextmanager
def _isolated_test_schema(url: URL) -> Iterator[URL]:
    admin_engine = create_engine(url)
    try:
        if inspect(admin_engine).has_schema(TEST_SCHEMA):
            pytest.fail(f"temporary schema {TEST_SCHEMA} already exists")

        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(TEST_SCHEMA))

        schema_url = url.update_query_dict({"options": f"-csearch_path={TEST_SCHEMA}"})
        try:
            yield schema_url
        finally:
            with admin_engine.begin() as connection:
                connection.execute(DropSchema(TEST_SCHEMA, cascade=True))
    finally:
        admin_engine.dispose()


def test_postgresql_migration_round_trip_preserves_existing_schema() -> None:
    url = _test_database_url()
    with _isolated_test_schema(url) as schema_url:
        engine = create_engine(schema_url)
        configuration = _alembic_config(schema_url)
        probe_metadata = MetaData()
        probe = Table(
            "existing_schema_probe",
            probe_metadata,
            Column("id", Integer, primary_key=True),
        )

        try:
            assert inspect(engine).get_table_names() == []

            command.upgrade(configuration, "head")
            assert _current_revision(engine) == ALEMBIC_HEAD
            assert set(inspect(engine).get_table_names()) >= BUSINESS_TABLES

            command.downgrade(configuration, "base")
            assert _current_revision(engine) is None

            command.upgrade(configuration, ALEMBIC_PREVIOUS)
            probe.create(engine)
            command.upgrade(configuration, "head")

            assert _current_revision(engine) == ALEMBIC_HEAD
            assert "existing_schema_probe" in inspect(engine).get_table_names()

            command.downgrade(configuration, ALEMBIC_PREVIOUS)
            assert "change_request" not in inspect(engine).get_table_names()
            assert "change_request_evidence" not in inspect(engine).get_table_names()
            assert "scope_approval" in inspect(engine).get_table_names()
            assert "scope_version" in inspect(engine).get_table_names()
            assert "capture_session" in inspect(engine).get_table_names()
            assert "media_asset" in inspect(engine).get_table_names()
            assert "participant_access_token" in inspect(engine).get_table_names()
            assert "move_job" in inspect(engine).get_table_names()
            assert "existing_schema_probe" in inspect(engine).get_table_names()

            command.downgrade(configuration, ALEMBIC_BASELINE)
            assert BUSINESS_TABLES.isdisjoint(inspect(engine).get_table_names())
            assert "existing_schema_probe" in inspect(engine).get_table_names()

            command.upgrade(configuration, "head")
            assert _current_revision(engine) == ALEMBIC_HEAD
        finally:
            engine.dispose()


@pytest.mark.anyio
async def test_async_postgresql_engine_connects() -> None:
    url = _test_database_url()
    settings = Settings(database_url=SecretStr(url.render_as_string(hide_password=False)))
    engine = create_database_engine(settings)

    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_move_job_commands_round_trip_on_postgresql() -> None:
    url = _test_database_url()
    with _isolated_test_schema(url) as schema_url:
        configuration = _alembic_config(schema_url)
        command.upgrade(configuration, "head")
        settings = Settings(
            database_url=SecretStr(schema_url.render_as_string(hide_password=False))
        )
        engine = create_database_engine(settings)
        factory = create_session_factory(engine)
        command_data = MoveJobCreate(
            title="강남 이사",
            participants=(ParticipantCreate(role=ParticipantRole.CUSTOMER, display_name="고객"),),
            locations=(
                LocationCreate(
                    kind=LocationKind.ORIGIN,
                    label="출발지",
                    room_zones=(RoomZoneCreate(name="거실", sort_order=0),),
                ),
            ),
        )

        try:
            async with transactional_session(factory) as session:
                created = await create_move_job(session, command_data)

            async with transactional_session(factory) as session:
                connected = await connect_participant(
                    session,
                    created.job.id,
                    ParticipantCreate(
                        role=ParticipantRole.FIELD_WORKER,
                        display_name="현장 담당",
                    ),
                )

            async with transactional_session(factory) as session:
                loaded = await get_move_job(session, created.job.id)

            assert connected.job == loaded
            assert [participant.role for participant in loaded.participants] == [
                ParticipantRole.CUSTOMER,
                ParticipantRole.FIELD_WORKER,
            ]
        finally:
            await engine.dispose()


@pytest.mark.anyio
async def test_capture_upload_and_analysis_draft_round_trip_on_postgresql() -> None:
    url = _test_database_url()
    with _isolated_test_schema(url) as schema_url:
        command.upgrade(_alembic_config(schema_url), "head")
        settings = Settings(
            database_url=SecretStr(schema_url.render_as_string(hide_password=False))
        )
        engine = create_database_engine(settings)
        factory = create_session_factory(engine)
        storage = FakeObjectStorage()
        job_command = MoveJobCreate(
            title="촬영 통합 테스트",
            participants=(ParticipantCreate(role=ParticipantRole.CUSTOMER, display_name="고객"),),
            locations=(
                LocationCreate(
                    kind=LocationKind.ORIGIN,
                    label="출발지",
                    room_zones=(RoomZoneCreate(name="거실", sort_order=0),),
                ),
            ),
        )

        try:
            async with transactional_session(factory) as session:
                created = await create_move_job(session, job_command)
                capture = await create_capture_session(
                    session,
                    created.job.id,
                    created.job.participants[0].id,
                )
                upload = await create_media_upload(
                    session,
                    storage,
                    created.job.id,
                    capture.id,
                    created.job.participants[0].id,
                    MediaUploadCreate(
                        room_zone_id=created.job.locations[0].room_zones[0].id,
                        media_purpose=MediaPurpose.INVENTORY,
                        content_type="image/jpeg",
                        content_length=10,
                    ),
                )

            object_key = upload.upload_url.removeprefix("https://storage.invalid/upload/")
            storage.metadata[object_key] = StorageObjectMetadata(
                object_key=object_key,
                content_type="image/jpeg",
                size_bytes=10,
                sha256_hex="b" * 64,
            )
            async with transactional_session(factory) as session:
                completed = await complete_media_upload(
                    session,
                    storage,
                    created.job.id,
                    capture.id,
                    upload.asset.id,
                    created.job.participants[0].id,
                )

            assert completed.status is MediaAssetStatus.UPLOADED
            assert completed.actual_size_bytes == 10
            assert completed.sha256_hex == "b" * 64

            result = AnalysisResult(
                analysis_run_id=AnalysisRunId(uuid4()),
                capture_session_id=CaptureSessionId(capture.id),
                model_name="gemini",
                model_version="2.5",
                prompt_version="scope-v1",
                draft_items=(
                    DraftItem(
                        item_key="sofa",
                        description="소파 이동",
                        confidence=0.9,
                        source_media_asset_ids=(MediaAssetId(upload.asset.id),),
                    ),
                ),
            )
            async with transactional_session(factory) as session:
                imported = await import_analysis_draft(session, created.job.id, result)

            assert imported.created_by_participant_id is None
            assert imported.analysis_source == result
        finally:
            await engine.dispose()

        downgraded_engine = create_engine(schema_url)
        try:
            command.downgrade(_alembic_config(schema_url), ALEMBIC_ANALYSIS_PREVIOUS)
            scope_version = Table("scope_version", MetaData(), autoload_with=downgraded_engine)
            with downgraded_engine.connect() as connection:
                restored_creator = connection.scalar(
                    select(scope_version.c.created_by_participant_id).where(
                        scope_version.c.id == imported.id
                    )
                )
            assert restored_creator == created.job.participants[0].id
        finally:
            downgraded_engine.dispose()


@pytest.mark.anyio
async def test_scope_version_concurrent_children_allow_one_winner_on_postgresql() -> None:
    url = _test_database_url()
    with _isolated_test_schema(url) as schema_url:
        command.upgrade(_alembic_config(schema_url), "head")
        settings = Settings(
            database_url=SecretStr(schema_url.render_as_string(hide_password=False))
        )
        engine = create_database_engine(settings)
        factory = create_session_factory(engine)
        job_command = MoveJobCreate(
            title="작업범위 동시성 테스트",
            participants=(ParticipantCreate(role=ParticipantRole.CUSTOMER, display_name="고객"),),
            locations=(
                LocationCreate(
                    kind=LocationKind.ORIGIN,
                    label="출발지",
                    room_zones=(RoomZoneCreate(name="거실", sort_order=0),),
                ),
            ),
        )

        try:
            async with transactional_session(factory) as session:
                created = await create_move_job(session, job_command)
                participant_id = created.job.participants[0].id
                room_zone_id = created.job.locations[0].room_zones[0].id
                root = await create_scope_version(
                    session,
                    created.job.id,
                    participant_id,
                    ScopeVersionCreate(
                        content=ScopeContent(
                            items=(
                                ScopeItem(
                                    item_key="sofa",
                                    room_zone_id=room_zone_id,
                                    description="소파 운반",
                                ),
                            )
                        )
                    ),
                )

            async def append_child(description: str) -> str:
                try:
                    async with transactional_session(factory) as session:
                        await create_scope_version(
                            session,
                            created.job.id,
                            participant_id,
                            ScopeVersionCreate(
                                parent_version_id=root.id,
                                content=ScopeContent(
                                    items=(
                                        ScopeItem(
                                            item_key="sofa",
                                            room_zone_id=room_zone_id,
                                            description=description,
                                        ),
                                    )
                                ),
                            ),
                        )
                    return "created"
                except ScopeVersionConflictError:
                    return "conflict"

            outcomes = await gather(
                append_child("소파 포장과 운반"),
                append_child("소파 분해와 운반"),
            )
            async with transactional_session(factory) as session:
                versions = await list_scope_versions(session, created.job.id)

            assert sorted(outcomes) == ["conflict", "created"]
            assert [version.sequence_number for version in versions] == [1, 2]
            assert versions[1].parent_version_id == root.id
        finally:
            await engine.dispose()


@pytest.mark.anyio
async def test_scope_lock_and_edit_race_allow_one_winner_on_postgresql() -> None:
    url = _test_database_url()
    with _isolated_test_schema(url) as schema_url:
        command.upgrade(_alembic_config(schema_url), "head")
        settings = Settings(
            database_url=SecretStr(schema_url.render_as_string(hide_password=False))
        )
        engine = create_database_engine(settings)
        factory = create_session_factory(engine)
        job_command = MoveJobCreate(
            title="확인과 편집 동시성 테스트",
            participants=(
                ParticipantCreate(role=ParticipantRole.CUSTOMER, display_name="고객"),
                ParticipantCreate(
                    role=ParticipantRole.COMPANY_MANAGER,
                    display_name="관리자",
                ),
            ),
            locations=(
                LocationCreate(
                    kind=LocationKind.ORIGIN,
                    label="출발지",
                    room_zones=(RoomZoneCreate(name="거실", sort_order=0),),
                ),
            ),
        )

        try:
            async with transactional_session(factory) as session:
                created = await create_move_job(session, job_command)
                customer = next(
                    participant
                    for participant in created.job.participants
                    if participant.role is ParticipantRole.CUSTOMER
                )
                manager = next(
                    participant
                    for participant in created.job.participants
                    if participant.role is ParticipantRole.COMPANY_MANAGER
                )
                room_zone_id = created.job.locations[0].room_zones[0].id
                root = await create_scope_version(
                    session,
                    created.job.id,
                    customer.id,
                    ScopeVersionCreate(
                        content=ScopeContent(
                            items=(
                                ScopeItem(
                                    item_key="sofa",
                                    room_zone_id=room_zone_id,
                                    description="소파 운반",
                                ),
                            )
                        )
                    ),
                )
                await approve_scope_version(
                    session,
                    created.job.id,
                    root.id,
                    customer.id,
                    ParticipantRole.CUSTOMER,
                )

            async def finish_approval() -> str:
                try:
                    async with transactional_session(factory) as session:
                        await approve_scope_version(
                            session,
                            created.job.id,
                            root.id,
                            manager.id,
                            ParticipantRole.COMPANY_MANAGER,
                        )
                    return "locked"
                except ScopeApprovalConflictError:
                    return "approval_conflict"

            async def append_child() -> str:
                try:
                    async with transactional_session(factory) as session:
                        await create_scope_version(
                            session,
                            created.job.id,
                            customer.id,
                            ScopeVersionCreate(
                                parent_version_id=root.id,
                                content=ScopeContent(
                                    items=(
                                        ScopeItem(
                                            item_key="sofa",
                                            room_zone_id=room_zone_id,
                                            description="소파 포장과 운반",
                                        ),
                                    )
                                ),
                            ),
                        )
                    return "edited"
                except ScopeVersionConflictError:
                    return "edit_conflict"

            outcomes = await gather(finish_approval(), append_child())
            async with transactional_session(factory) as session:
                versions = await list_scope_versions(session, created.job.id)

            assert sorted(outcomes) in (
                ["edit_conflict", "locked"],
                ["approval_conflict", "edited"],
            )
            if outcomes[0] == "locked":
                assert len(versions) == 1
                assert versions[0].locked_at is not None
            else:
                assert len(versions) == 2
                assert versions[0].locked_at is None
                assert versions[1].parent_version_id == root.id
        finally:
            await engine.dispose()


@pytest.mark.anyio
async def test_change_request_concurrent_approvals_allow_one_result_on_postgresql() -> None:
    url = _test_database_url()
    with _isolated_test_schema(url) as schema_url:
        command.upgrade(_alembic_config(schema_url), "head")
        settings = Settings(
            database_url=SecretStr(schema_url.render_as_string(hide_password=False))
        )
        engine = create_database_engine(settings)
        factory = create_session_factory(engine)
        storage = FakeObjectStorage()
        job_command = MoveJobCreate(
            title="Change approval race",
            participants=(
                ParticipantCreate(role=ParticipantRole.CUSTOMER, display_name="Customer"),
                ParticipantCreate(
                    role=ParticipantRole.COMPANY_MANAGER,
                    display_name="Manager",
                ),
                ParticipantCreate(
                    role=ParticipantRole.FIELD_WORKER,
                    display_name="Field worker",
                ),
            ),
            locations=(
                LocationCreate(
                    kind=LocationKind.ORIGIN,
                    label="Origin",
                    room_zones=(RoomZoneCreate(name="Living room", sort_order=0),),
                ),
            ),
        )

        try:
            async with transactional_session(factory) as session:
                created = await create_move_job(session, job_command)
                participants = {
                    participant.role: participant for participant in created.job.participants
                }
                customer = participants[ParticipantRole.CUSTOMER]
                manager = participants[ParticipantRole.COMPANY_MANAGER]
                worker = participants[ParticipantRole.FIELD_WORKER]
                room_zone_id = created.job.locations[0].room_zones[0].id
                root = await create_scope_version(
                    session,
                    created.job.id,
                    customer.id,
                    ScopeVersionCreate(
                        content=ScopeContent(
                            items=(
                                ScopeItem(
                                    item_key="sofa",
                                    room_zone_id=room_zone_id,
                                    description="Original sofa",
                                ),
                            )
                        )
                    ),
                )
                await approve_scope_version(
                    session,
                    created.job.id,
                    root.id,
                    customer.id,
                    ParticipantRole.CUSTOMER,
                )
                await approve_scope_version(
                    session,
                    created.job.id,
                    root.id,
                    manager.id,
                    ParticipantRole.COMPANY_MANAGER,
                )
                capture = await create_capture_session(
                    session,
                    created.job.id,
                    worker.id,
                )
                upload = await create_media_upload(
                    session,
                    storage,
                    created.job.id,
                    capture.id,
                    worker.id,
                    MediaUploadCreate(
                        room_zone_id=room_zone_id,
                        media_purpose=MediaPurpose.CHANGE_EVIDENCE,
                        content_type="image/jpeg",
                        content_length=12,
                    ),
                )

            object_key = upload.upload_url.removeprefix("https://storage.invalid/upload/")
            storage.metadata[object_key] = StorageObjectMetadata(
                object_key=object_key,
                content_type="image/jpeg",
                size_bytes=12,
                sha256_hex="c" * 64,
            )
            async with transactional_session(factory) as session:
                await complete_media_upload(
                    session,
                    storage,
                    created.job.id,
                    capture.id,
                    upload.asset.id,
                    worker.id,
                )
                requests = []
                for suffix in ("A", "B"):
                    requests.append(
                        await create_change_request(
                            session,
                            created.job.id,
                            worker.id,
                            ChangeRequestCreate(
                                base_scope_version_id=root.id,
                                description=f"Change request {suffix}",
                                proposed_content=ScopeContent(
                                    items=(
                                        ScopeItem(
                                            item_key="sofa",
                                            room_zone_id=room_zone_id,
                                            description=f"Changed sofa {suffix}",
                                        ),
                                    )
                                ),
                                evidence_media_asset_ids=(upload.asset.id,),
                            ),
                        )
                    )

            async def approve_change(request_id: UUID, participant_id: UUID) -> str:
                try:
                    async with transactional_session(factory) as session:
                        await decide_change_request(
                            session,
                            created.job.id,
                            request_id,
                            participant_id,
                            ChangeDecisionCreate(decision="approve"),
                        )
                    return "approved"
                except ChangeRequestConflictError:
                    return "conflict"

            outcomes = await gather(
                approve_change(requests[0].id, customer.id),
                approve_change(requests[1].id, manager.id),
            )
            async with transactional_session(factory) as session:
                stored_requests = await list_change_requests(session, created.job.id)
                versions = await list_scope_versions(session, created.job.id)

            assert sorted(outcomes) == ["approved", "conflict"]
            assert [request.status for request in stored_requests].count(
                ChangeRequestStatus.APPROVED
            ) == 1
            assert [request.status for request in stored_requests].count(
                ChangeRequestStatus.PENDING
            ) == 1
            assert (
                sum(request.result_scope_version_id is not None for request in stored_requests) == 1
            )
            assert len(versions) == 2
            assert versions[1].parent_version_id == root.id
        finally:
            await engine.dispose()
