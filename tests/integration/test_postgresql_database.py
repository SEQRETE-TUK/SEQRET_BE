"""PostgreSQL integration tests for migrations and async connectivity."""

import os
import sys
from asyncio import SelectorEventLoop
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from app.config import Settings
from app.contracts.actor import ParticipantRole
from app.modules.move_job.models import LocationKind
from app.modules.move_job.schemas import (
    LocationCreate,
    MoveJobCreate,
    ParticipantCreate,
    RoomZoneCreate,
)
from app.modules.move_job.service import connect_participant, create_move_job, get_move_job
from app.platform.db import create_database_engine, create_session_factory, transactional_session

ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_BASELINE = "fnd_a02_0001"
ALEMBIC_PREVIOUS = "a_01_0001"
ALEMBIC_HEAD = "a_02_0001"
BUSINESS_TABLES = {
    "job_participant",
    "location",
    "move_job",
    "participant_access_token",
    "room_zone",
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

            command.upgrade(configuration, ALEMBIC_BASELINE)
            probe.create(engine)
            command.upgrade(configuration, "head")

            assert _current_revision(engine) == ALEMBIC_HEAD
            assert "existing_schema_probe" in inspect(engine).get_table_names()

            command.downgrade(configuration, ALEMBIC_PREVIOUS)
            assert "participant_access_token" not in inspect(engine).get_table_names()
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
