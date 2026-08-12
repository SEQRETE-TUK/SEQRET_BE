"""PostgreSQL integration tests for migrations and async connectivity."""

import os
import sys
from asyncio import Barrier, Event, SelectorEventLoop, create_task, gather, wait_for
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
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
from app.contracts.events import DomainEventType
from app.contracts.fakes import FakeObjectStorage
from app.contracts.maintenance import (
    MediaDeletionOutcome,
    MediaDeletionResultV1,
    MediaDeletionTaskV1,
)
from app.contracts.media import MediaAssetStatus, MediaPurpose
from app.contracts.ports import StorageObjectMetadata
from app.contracts.primitives import (
    AnalysisRunId,
    BackgroundJobId,
    CaptureSessionId,
    MediaAssetId,
)
from app.modules.access.models import ParticipantAccessToken
from app.modules.access.service import (
    InvalidAccessTokenError,
    _increment_database_rate_window,
    load_access_link,
    revoke_access_link,
    rotate_access_link,
)
from app.modules.background_job.models import BackgroundJob, BackgroundJobStatus
from app.modules.background_job.service import (
    BackgroundJobConflictError,
    claim_background_jobs,
    complete_media_deletion,
    create_retention_background_job,
    retry_background_job,
    start_media_deletion,
)
from app.modules.capture.models import CaptureSession, MediaAsset
from app.modules.capture.schemas import MediaUploadCreate
from app.modules.capture.service import (
    complete_media_upload,
    create_capture_session,
    create_media_upload,
)
from app.modules.completion.models import AuditEventType
from app.modules.completion.schemas import CompletionConfirmationCreate
from app.modules.completion.service import (
    CompletionConflictError,
    confirm_completion,
    list_audit_events,
    list_completion_confirmations,
)
from app.modules.move_job.models import JobParticipant, LocationKind, MoveJob, MoveJobStatus
from app.modules.move_job.schemas import (
    LocationCreate,
    MoveJobCreate,
    ParticipantCreate,
    RoomZoneCreate,
)
from app.modules.move_job.service import create_move_job, get_move_job
from app.modules.scope.models import ChangeRequest, ChangeRequestStatus, ScopeApproval, ScopeVersion
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
from app.platform.event_bus.models import OutboxEvent
from app.platform.event_bus.service import claim_outbox_events, enqueue_domain_event

ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_BASELINE = "fnd_a02_0001"
ALEMBIC_ANALYSIS_PREVIOUS = "a_05_0001"
ALEMBIC_CHANGE_PREVIOUS = "a_06_0001"
ALEMBIC_PREVIOUS = "a_07_0001"
ALEMBIC_HEAD = "a_12_0001"
ALEMBIC_OUTBOX_PREVIOUS = "a_08_0001"
ALEMBIC_RATE_LIMIT_PREVIOUS = "a_09_0001"
ALEMBIC_BACKGROUND_JOB_PREVIOUS = "a_10_0001"
BUSINESS_TABLES = {
    "background_job",
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
    "completion_confirmation",
    "completion_evidence",
    "audit_event",
    "outbox_event",
    "event_consumption",
    "notification_delivery",
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

            command.downgrade(configuration, ALEMBIC_RATE_LIMIT_PREVIOUS)
            access_columns = {
                column["name"] for column in inspect(engine).get_columns("participant_access_token")
            }
            assert "rate_window_started_at" not in access_columns
            assert "rate_window_count" not in access_columns
            command.upgrade(configuration, "head")

            command.downgrade(configuration, ALEMBIC_BACKGROUND_JOB_PREVIOUS)
            assert "background_job" not in inspect(engine).get_table_names()
            command.upgrade(configuration, "head")

            command.downgrade(configuration, ALEMBIC_OUTBOX_PREVIOUS)
            assert "outbox_event" not in inspect(engine).get_table_names()
            assert "event_consumption" not in inspect(engine).get_table_names()
            assert "notification_delivery" not in inspect(engine).get_table_names()

            command.downgrade(configuration, ALEMBIC_PREVIOUS)
            assert "completion_confirmation" not in inspect(engine).get_table_names()
            assert "completion_evidence" not in inspect(engine).get_table_names()
            assert "audit_event" not in inspect(engine).get_table_names()
            assert "completed_at" not in {
                column["name"] for column in inspect(engine).get_columns("move_job")
            }

            command.downgrade(configuration, ALEMBIC_CHANGE_PREVIOUS)
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
async def test_database_rate_fallback_increments_atomically_on_postgresql() -> None:
    url = _test_database_url()
    with _isolated_test_schema(url) as schema_url:
        command.upgrade(_alembic_config(schema_url), "head")
        settings = Settings(
            database_url=SecretStr(schema_url.render_as_string(hide_password=False))
        )
        engine = create_database_engine(settings)
        factory = create_session_factory(engine)
        command_data = MoveJobCreate(
            title="Rate fallback race",
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
                created = await create_move_job(session, command_data)
                access_link_id = created.access_links[0].id
                participant_id = created.access_links[0].participant_id
                original_secret = created.access_links[0].secret
                stored = await session.get(ParticipantAccessToken, access_link_id)
                assert stored is not None
                original_hash = stored.token_hash

            now = datetime.now(UTC)

            async def increment() -> tuple[int, datetime]:
                async with transactional_session(factory) as session:
                    return await _increment_database_rate_window(
                        session,
                        access_link_id,
                        expected_token_hash=original_hash,
                        now=now,
                        window_seconds=60,
                    )

            rate_windows = await gather(*(increment() for _ in range(20)))

            assert sorted(count for count, _ in rate_windows) == list(range(1, 21))
            assert {started_at for _, started_at in rate_windows} == {now}
            async with transactional_session(factory) as session:
                stored = await session.get(ParticipantAccessToken, access_link_id)
                assert stored is not None
                assert stored.rate_window_count == 20
                assert stored.rate_window_started_at == now

            rotation_barrier = Barrier(2)

            async def rotate() -> str:
                try:
                    async with transactional_session(factory) as session:
                        participant = await session.get(JobParticipant, participant_id)
                        assert participant is not None
                        await rotation_barrier.wait()
                        await rotate_access_link(
                            session,
                            participant,
                            current_secret=original_secret,
                            actor_participant_id=participant_id,
                        )
                    return "rotated"
                except InvalidAccessTokenError:
                    return "stale"

            assert sorted(await gather(rotate(), rotate())) == ["rotated", "stale"]
            async with transactional_session(factory) as session:
                with pytest.raises(InvalidAccessTokenError):
                    await _increment_database_rate_window(
                        session,
                        access_link_id,
                        expected_token_hash=original_hash,
                        now=now,
                        window_seconds=60,
                    )
        finally:
            await engine.dispose()


@pytest.mark.anyio
async def test_concurrent_access_link_revocation_records_one_audit_on_postgresql() -> None:
    url = _test_database_url()
    with _isolated_test_schema(url) as schema_url:
        command.upgrade(_alembic_config(schema_url), "head")
        settings = Settings(
            database_url=SecretStr(schema_url.render_as_string(hide_password=False))
        )
        engine = create_database_engine(settings)
        factory = create_session_factory(engine)
        command_data = MoveJobCreate(
            title="Concurrent access-link revocation",
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
                created = await create_move_job(session, command_data)
                access_link_id = created.access_links[0].id
                actor_participant_id = created.access_links[0].participant_id

            revoke_barrier = Barrier(2)

            async def revoke() -> None:
                async with transactional_session(factory) as session:
                    access_link = await load_access_link(
                        session,
                        created.job.id,
                        access_link_id,
                    )
                    assert access_link is not None
                    await wait_for(revoke_barrier.wait(), timeout=5)
                    await revoke_access_link(session, access_link, actor_participant_id)

            await wait_for(gather(revoke(), revoke()), timeout=10)

            async with transactional_session(factory) as session:
                stored = await session.get(ParticipantAccessToken, access_link_id)
                events = await list_audit_events(session, created.job.id)

            assert stored is not None
            assert stored.revoked_at is not None
            assert [event.event_type for event in events].count(
                AuditEventType.ACCESS_LINK_REVOKED
            ) == 1
        finally:
            await engine.dispose()


@pytest.mark.anyio
async def test_outbox_claims_are_exclusive_on_postgresql() -> None:
    url = _test_database_url()
    with _isolated_test_schema(url) as schema_url:
        command.upgrade(_alembic_config(schema_url), "head")
        settings = Settings(
            database_url=SecretStr(schema_url.render_as_string(hide_password=False))
        )
        engine = create_database_engine(settings)
        factory = create_session_factory(engine)
        job_id = uuid4()
        ready = Event()
        release = Event()

        async def claim(hold_lock: bool) -> tuple[UUID, ...]:
            async with factory.begin() as session:
                claims = await claim_outbox_events(session, limit=1, lease_seconds=60)
                if hold_lock:
                    ready.set()
                    await release.wait()
                return tuple(claim.event.event_id for claim in claims)

        try:
            async with factory.begin() as session:
                enqueue_domain_event(
                    session,
                    DomainEventType.SCOPE_LOCKED_V1,
                    job_id,
                    trace_id="0123456789abcdef0123456789abcdef",
                    payload={"scope_version_id": str(uuid4()), "content_hash": "a" * 64},
                )

            first_task = claim(True)

            async def second_claim() -> tuple[UUID, ...]:
                await ready.wait()
                try:
                    return await claim(False)
                finally:
                    release.set()

            first, second = await gather(first_task, second_claim())
            assert len(first) == 1
            assert second == ()
            async with factory() as session:
                row = await session.get(OutboxEvent, first[0])
                assert row is not None
                assert row.attempt_count == 1
                assert row.lock_token is not None
        finally:
            await engine.dispose()


@pytest.mark.anyio
async def test_background_job_create_and_claim_are_exclusive_on_postgresql() -> None:
    url = _test_database_url()
    with _isolated_test_schema(url) as schema_url:
        command.upgrade(_alembic_config(schema_url), "head")
        settings = Settings(
            database_url=SecretStr(schema_url.render_as_string(hide_password=False))
        )
        engine = create_database_engine(settings)
        factory = create_session_factory(engine)
        operation_time = datetime.now(UTC)
        command_data = MoveJobCreate(
            title="Background job race",
            participants=(
                ParticipantCreate(
                    role=ParticipantRole.COMPANY_MANAGER,
                    display_name="Manager",
                ),
                ParticipantCreate(role=ParticipantRole.CUSTOMER, display_name="Customer"),
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
                created = await create_move_job(session, command_data)
                participant_id = created.job.participants[0].id
                capture = await create_capture_session(
                    session,
                    created.job.id,
                    participant_id,
                )
                move_job = await session.get(MoveJob, created.job.id)
                assert move_job is not None
                move_job.status = MoveJobStatus.COMPLETED
                move_job.completed_at = operation_time - timedelta(days=31)
                asset = MediaAsset(
                    capture_session_id=capture.id,
                    room_zone_id=created.job.locations[0].room_zones[0].id,
                    media_purpose=MediaPurpose.COMPLETION,
                    status=MediaAssetStatus.UPLOADED,
                    object_key=f"jobs/{created.job.id}/retention/{uuid4()}",
                    content_type="image/jpeg",
                    expected_size_bytes=10,
                    actual_size_bytes=10,
                    generation="7",
                    uploaded_at=operation_time - timedelta(days=31),
                )
                session.add(asset)
                await session.flush()
                asset_id = asset.id

            async def create() -> UUID:
                async with transactional_session(factory) as session:
                    response = await create_retention_background_job(
                        session,
                        created.job.id,
                        asset_id,
                        participant_id,
                        retention_cutoff=operation_time - timedelta(days=30),
                        trace_id="0123456789abcdef0123456789abcdef",
                        scheduled_at=operation_time,
                    )
                    return response.id

            first_id, second_id = await gather(create(), create())
            assert first_id == second_id

            ready = Event()
            release = Event()

            async def claim(hold_lock: bool) -> tuple[UUID, ...]:
                async with factory.begin() as session:
                    claims = await claim_background_jobs(session, now=operation_time, limit=1)
                    if hold_lock:
                        ready.set()
                        await release.wait()
                    return tuple(UUID(str(item.task.background_job_id)) for item in claims)

            first_claim = claim(True)

            async def second_claim() -> tuple[UUID, ...]:
                await ready.wait()
                try:
                    return await claim(False)
                finally:
                    release.set()

            claimed, skipped = await gather(first_claim, second_claim())
            assert claimed == (first_id,)
            assert skipped == ()
            async with factory() as session:
                rows = (await session.scalars(select(BackgroundJob))).all()
                assert len(rows) == 1
                assert rows[0].attempt_count == 1

            task = MediaDeletionTaskV1(
                background_job_id=BackgroundJobId(first_id),
                attempt_count=1,
                trace_id="0123456789abcdef0123456789abcdef",
            )
            async with transactional_session(factory) as session:
                await start_media_deletion(
                    session,
                    task,
                    now=operation_time - timedelta(minutes=16),
                )

            async def retry_expired() -> str:
                try:
                    async with transactional_session(factory) as session:
                        await retry_background_job(
                            session,
                            created.job.id,
                            first_id,
                            now=operation_time,
                        )
                except BackgroundJobConflictError:
                    return "conflict"
                return "retried"

            async def complete_expired() -> str:
                result = MediaDeletionResultV1(
                    background_job_id=BackgroundJobId(first_id),
                    attempt_count=1,
                    outcome=MediaDeletionOutcome.SUCCEEDED,
                )
                try:
                    async with transactional_session(factory) as session:
                        await complete_media_deletion(
                            session,
                            result,
                            completed_at=operation_time,
                        )
                except BackgroundJobConflictError:
                    return "conflict"
                return "completed"

            terminal_outcomes = await gather(retry_expired(), complete_expired())
            assert terminal_outcomes.count("conflict") == 1
            assert set(terminal_outcomes) in (
                {"conflict", "retried"},
                {"completed", "conflict"},
            )
            async with factory() as session:
                row = await session.get(BackgroundJob, first_id)
                stored_asset = await session.get(MediaAsset, asset_id)
                events = (
                    await session.scalars(
                        select(OutboxEvent).where(
                            OutboxEvent.aggregate_id == created.job.id,
                            OutboxEvent.event_type == DomainEventType.MEDIA_DELETED_V1,
                        )
                    )
                ).all()
                assert row is not None and stored_asset is not None
                if "completed" in terminal_outcomes:
                    assert row.status is BackgroundJobStatus.SUCCEEDED
                    assert stored_asset.status is MediaAssetStatus.DELETED
                    assert len(events) == 1
                else:
                    assert row.status is BackgroundJobStatus.PENDING
                    assert stored_asset.status is MediaAssetStatus.UPLOADED
                    assert events == []
        finally:
            await engine.dispose()


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
                    room_zones=(RoomZoneCreate(name="거실", sort_order=0),),
                ),
            ),
        )

        try:
            async with transactional_session(factory) as session:
                created = await create_move_job(session, command_data)

            async with transactional_session(factory) as session:
                loaded = await get_move_job(session, created.job.id)

            assert created.job == loaded
            assert [participant.role for participant in loaded.participants] == [
                ParticipantRole.COMPANY_MANAGER,
                ParticipantRole.CUSTOMER,
                ParticipantRole.FIELD_WORKER,
            ]
        finally:
            await engine.dispose()


@pytest.mark.anyio
async def test_completion_confirmations_serialize_and_complete_once_on_postgresql() -> None:
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
            title="Completion race",
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
                for participant, role in (
                    (customer, ParticipantRole.CUSTOMER),
                    (manager, ParticipantRole.COMPANY_MANAGER),
                ):
                    await approve_scope_version(
                        session,
                        created.job.id,
                        root.id,
                        participant.id,
                        role,
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
                        media_purpose=MediaPurpose.COMPLETION,
                        content_type="image/jpeg",
                        content_length=16,
                    ),
                )

            object_key = upload.upload_url.removeprefix("https://storage.invalid/upload/")
            storage.metadata[object_key] = StorageObjectMetadata(
                object_key=object_key,
                content_type="image/jpeg",
                size_bytes=16,
                sha256_hex="f" * 64,
                generation="7",
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

            completion = CompletionConfirmationCreate(
                scope_version_id=root.id,
                evidence_media_asset_ids=(upload.asset.id,),
            )

            async def confirm(participant_id: UUID, role: ParticipantRole) -> str:
                try:
                    async with transactional_session(factory) as session:
                        await confirm_completion(
                            session,
                            created.job.id,
                            participant_id,
                            role,
                            completion,
                            retention_days=30,
                            trace_id="0123456789abcdef0123456789abcdef",
                        )
                    return "confirmed"
                except CompletionConflictError:
                    return "conflict"

            outcomes = await gather(
                confirm(customer.id, ParticipantRole.CUSTOMER),
                confirm(manager.id, ParticipantRole.COMPANY_MANAGER),
            )
            async with transactional_session(factory) as session:
                confirmations = await list_completion_confirmations(
                    session,
                    created.job.id,
                )
                events = await list_audit_events(session, created.job.id)
                loaded = await get_move_job(session, created.job.id)
                retention_jobs = (
                    await session.scalars(
                        select(BackgroundJob).where(BackgroundJob.move_job_id == created.job.id)
                    )
                ).all()

            assert tuple(outcomes) == ("confirmed", "confirmed")
            assert {confirmation.role for confirmation in confirmations} == {
                ParticipantRole.CUSTOMER,
                ParticipantRole.COMPANY_MANAGER,
            }
            assert loaded.status is MoveJobStatus.COMPLETED
            assert loaded.completed_at is not None
            assert [event.event_type for event in events].count(AuditEventType.JOB_COMPLETED) == 1
            assert len(retention_jobs) == 1
            assert retention_jobs[0].status is BackgroundJobStatus.PENDING
            assert retention_jobs[0].scheduled_at == loaded.completed_at + timedelta(days=30)
        finally:
            await engine.dispose()


@pytest.mark.anyio
async def test_capture_upload_and_analysis_draft_round_trip_on_postgresql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
                capture = await create_capture_session(
                    session,
                    created.job.id,
                    customer.id,
                )
                upload = await create_media_upload(
                    session,
                    storage,
                    created.job.id,
                    capture.id,
                    customer.id,
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
                generation="1",
            )
            async with transactional_session(factory) as session:
                completed = await complete_media_upload(
                    session,
                    storage,
                    created.job.id,
                    capture.id,
                    upload.asset.id,
                    customer.id,
                )

            assert completed.status is MediaAssetStatus.UPLOADED
            assert completed.actual_size_bytes == 10
            assert completed.sha256_hex == "b" * 64

            async with transactional_session(factory) as session:
                completion_upload = await create_media_upload(
                    session,
                    storage,
                    created.job.id,
                    capture.id,
                    customer.id,
                    MediaUploadCreate(
                        room_zone_id=created.job.locations[0].room_zones[0].id,
                        media_purpose=MediaPurpose.COMPLETION,
                        content_type="image/jpeg",
                        content_length=12,
                    ),
                )

            completion_object_key = completion_upload.upload_url.removeprefix(
                "https://storage.invalid/upload/"
            )
            storage.metadata[completion_object_key] = StorageObjectMetadata(
                object_key=completion_object_key,
                content_type="image/jpeg",
                size_bytes=12,
                sha256_hex="c" * 64,
                generation="2",
            )
            metadata_barrier = Barrier(2)
            original_get_metadata = storage.get_metadata

            async def synchronized_metadata(
                *, object_key: str, timeout_seconds: float
            ) -> StorageObjectMetadata:
                metadata = await original_get_metadata(
                    object_key=object_key,
                    timeout_seconds=timeout_seconds,
                )
                if metadata.object_key == completion_object_key:
                    await wait_for(metadata_barrier.wait(), timeout=5)
                return metadata

            monkeypatch.setattr(storage, "get_metadata", synchronized_metadata)

            async def complete_concurrently() -> MediaAssetStatus:
                async with transactional_session(factory) as session:
                    result = await complete_media_upload(
                        session,
                        storage,
                        created.job.id,
                        capture.id,
                        completion_upload.asset.id,
                        customer.id,
                        trace_id="0123456789abcdef0123456789abcdef",
                    )
                    return result.status

            completion_outcomes = await gather(
                complete_concurrently(),
                complete_concurrently(),
            )
            async with transactional_session(factory) as session:
                stored_completion_asset = await session.get(
                    MediaAsset,
                    completion_upload.asset.id,
                )
                completion_events = await list_audit_events(session, created.job.id)
                completion_outbox = (
                    await session.scalars(
                        select(OutboxEvent).where(
                            OutboxEvent.aggregate_id == created.job.id,
                            OutboxEvent.event_type == DomainEventType.COMPLETION_MEDIA_SUBMITTED_V1,
                        )
                    )
                ).all()

            assert tuple(completion_outcomes) == (
                MediaAssetStatus.UPLOADED,
                MediaAssetStatus.UPLOADED,
            )
            assert stored_completion_asset is not None
            assert stored_completion_asset.generation == "2"
            assert [event.event_type for event in completion_events].count(
                AuditEventType.COMPLETION_MEDIA_UPLOADED
            ) == 1
            assert len(completion_outbox) == 1

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
            assert restored_creator == customer.id
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
                    room_zones=(RoomZoneCreate(name="거실", sort_order=0),),
                ),
            ),
        )

        try:
            async with transactional_session(factory) as session:
                created = await create_move_job(session, job_command)
                participant_id = next(
                    participant.id
                    for participant in created.job.participants
                    if participant.role is ParticipantRole.CUSTOMER
                )
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
                ParticipantCreate(
                    role=ParticipantRole.FIELD_WORKER,
                    display_name="현장 담당",
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
                approval_roles = set(
                    (
                        await session.scalars(
                            select(ScopeApproval.role).where(
                                ScopeApproval.scope_version_id == root.id
                            )
                        )
                    ).all()
                )
                lock_events = (
                    await session.scalars(
                        select(OutboxEvent).where(
                            OutboxEvent.aggregate_id == created.job.id,
                            OutboxEvent.event_type == DomainEventType.SCOPE_LOCKED_V1,
                        )
                    )
                ).all()

            assert sorted(outcomes) in (
                ["edit_conflict", "locked"],
                ["approval_conflict", "edited"],
            )
            if outcomes[0] == "locked":
                assert len(versions) == 1
                assert versions[0].locked_at is not None
                assert approval_roles == {
                    ParticipantRole.CUSTOMER,
                    ParticipantRole.COMPANY_MANAGER,
                }
                assert len(lock_events) == 1
            else:
                assert len(versions) == 2
                assert versions[0].locked_at is None
                assert versions[1].parent_version_id == root.id
                assert approval_roles == {ParticipantRole.CUSTOMER}
                assert lock_events == []
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
                generation="1",
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


@pytest.mark.anyio
async def test_change_request_creation_and_scope_decision_avoid_deadlock_on_postgresql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = _test_database_url()
    with _isolated_test_schema(url) as schema_url:
        command.upgrade(_alembic_config(schema_url), "head")
        settings = Settings(
            database_url=SecretStr(schema_url.render_as_string(hide_password=False))
        )
        engine = create_database_engine(settings)
        factory = create_session_factory(engine)
        job_command = MoveJobCreate(
            title="Change request lock ordering",
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
                worker = participants[ParticipantRole.FIELD_WORKER]
                room_zone_id = created.job.locations[0].room_zones[0].id
                root_content = ScopeContent(
                    items=(
                        ScopeItem(
                            item_key="sofa",
                            room_zone_id=room_zone_id,
                            description="Original sofa",
                        ),
                    )
                )
                proposed_content = ScopeContent(
                    items=(
                        ScopeItem(
                            item_key="sofa",
                            room_zone_id=room_zone_id,
                            description="Replacement sofa",
                        ),
                    )
                )
                root = ScopeVersion(
                    id=uuid4(),
                    job_id=created.job.id,
                    sequence_number=1,
                    content=root_content.model_dump(mode="json"),
                    content_hash="a" * 64,
                    created_by_participant_id=customer.id,
                    locked_at=datetime.now(UTC),
                )
                capture = CaptureSession(
                    id=uuid4(),
                    job_id=created.job.id,
                    created_by_participant_id=worker.id,
                )
                change_asset = MediaAsset(
                    id=uuid4(),
                    capture_session_id=capture.id,
                    room_zone_id=room_zone_id,
                    media_purpose=MediaPurpose.CHANGE_EVIDENCE,
                    status=MediaAssetStatus.UPLOADED,
                    object_key=f"jobs/{created.job.id}/change.jpg",
                    content_type="image/jpeg",
                    expected_size_bytes=16,
                    actual_size_bytes=16,
                    sha256_hex="c" * 64,
                    generation="2",
                    uploaded_at=datetime.now(UTC),
                )
                change_request = ChangeRequest(
                    id=uuid4(),
                    job_id=created.job.id,
                    base_scope_version_id=root.id,
                    requested_by_participant_id=worker.id,
                    description="Replace the sofa",
                    proposed_content=proposed_content.model_dump(mode="json"),
                    status=ChangeRequestStatus.PENDING,
                )
                session.add_all((root, capture))
                await session.flush()
                session.add_all((change_asset, change_request))

            decision_flush_started = Event()
            release_decision = Event()
            change_job_lock_seen = Event()
            change_scope_lock_attempted = Event()
            create_command = ChangeRequestCreate(
                base_scope_version_id=root.id,
                description="Replace the sofa again",
                proposed_content=ScopeContent(
                    items=(
                        ScopeItem(
                            item_key="sofa",
                            room_zone_id=room_zone_id,
                            description="Another replacement sofa",
                        ),
                    )
                ),
                evidence_media_asset_ids=(change_asset.id,),
            )

            async with factory() as decision_session, factory() as create_session:
                original_decision_flush = decision_session.flush
                original_create_scalar = create_session.scalar

                async def decision_flush() -> None:
                    decision_flush_started.set()
                    await wait_for(release_decision.wait(), timeout=10)
                    await original_decision_flush()

                async def create_scalar(statement: Any) -> Any:
                    if "FROM move_job" in str(statement):
                        compiled = str(statement.compile(dialect=engine.dialect))
                        if "FOR NO KEY UPDATE" in compiled:
                            change_job_lock_seen.set()
                    if "FROM scope_version" in str(statement) and "FOR UPDATE" in str(statement):
                        change_scope_lock_attempted.set()
                    return await original_create_scalar(statement)

                monkeypatch.setattr(decision_session, "flush", decision_flush)
                monkeypatch.setattr(create_session, "scalar", create_scalar)

                async def approve() -> str:
                    async with decision_session.begin():
                        await decide_change_request(
                            decision_session,
                            created.job.id,
                            change_request.id,
                            customer.id,
                            ChangeDecisionCreate(decision="approve"),
                        )
                    return "approved"

                async def request_change() -> str:
                    try:
                        async with create_session.begin():
                            await create_change_request(
                                create_session,
                                created.job.id,
                                worker.id,
                                create_command,
                            )
                        return "created"
                    except ChangeRequestConflictError:
                        return "conflict"

                decision_task = create_task(approve())
                create_task_ = None
                try:
                    await wait_for(decision_flush_started.wait(), timeout=5)
                    create_task_ = create_task(request_change())
                    await wait_for(change_scope_lock_attempted.wait(), timeout=5)
                    assert change_job_lock_seen.is_set()
                except BaseException:
                    release_decision.set()
                    tasks = [decision_task]
                    if create_task_ is not None:
                        tasks.append(create_task_)
                    await wait_for(
                        gather(*tasks, return_exceptions=True),
                        timeout=10,
                    )
                    raise
                release_decision.set()
                assert create_task_ is not None
                outcomes = await wait_for(
                    gather(decision_task, create_task_),
                    timeout=10,
                )

            async with transactional_session(factory) as session:
                stored_requests = await list_change_requests(session, created.job.id)
                versions = await list_scope_versions(session, created.job.id)
                loaded = await get_move_job(session, created.job.id)

            assert tuple(outcomes) == ("approved", "conflict")
            assert loaded.status is MoveJobStatus.DRAFT
            assert len(stored_requests) == 1
            assert stored_requests[0].status is ChangeRequestStatus.APPROVED
            assert len(versions) == 2
            assert stored_requests[0].result_scope_version_id == versions[1].id
            assert versions[1].parent_version_id == root.id
        finally:
            await engine.dispose()
