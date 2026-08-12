"""Fast migration graph and SQLite compatibility tests."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_BASELINE = "fnd_a02_0001"
ALEMBIC_PREVIOUS = "a_05_0001"
ALEMBIC_HEAD = "a_06_0001"
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
}


def _alembic_config(database_url: str | None = None) -> Config:
    configuration = Config(str(ROOT / "alembic.ini"))
    if database_url is not None:
        configuration.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return configuration


def _current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()


def test_alembic_has_one_linear_head() -> None:
    script = ScriptDirectory.from_config(_alembic_config())

    assert script.get_heads() == [ALEMBIC_HEAD]


def test_migration_round_trip_preserves_existing_schema(tmp_path: Path) -> None:
    database_path = (tmp_path / "migration.sqlite3").as_posix()
    database_url = f"sqlite+pysqlite:///{database_path}"
    configuration = _alembic_config(database_url)
    engine = create_engine(database_url)
    probe_metadata = MetaData()
    Table("existing_schema_probe", probe_metadata, Column("id", Integer, primary_key=True))

    try:
        command.upgrade(configuration, "head")
        assert _current_revision(engine) == ALEMBIC_HEAD
        assert set(inspect(engine).get_table_names()) >= BUSINESS_TABLES

        command.downgrade(configuration, "base")
        assert _current_revision(engine) is None

        command.upgrade(configuration, ALEMBIC_PREVIOUS)
        probe_metadata.create_all(engine)
        command.upgrade(configuration, "head")

        assert _current_revision(engine) == ALEMBIC_HEAD
        assert "existing_schema_probe" in inspect(engine).get_table_names()

        migrated_metadata = MetaData()
        migrated_metadata.reflect(
            engine,
            only=("move_job", "job_participant", "capture_session", "scope_version"),
        )
        job_id = uuid4().hex
        participant_id = uuid4().hex
        capture_id = uuid4().hex
        scope_version_id = uuid4().hex
        created_at = datetime.now(UTC)
        with engine.begin() as connection:
            connection.execute(
                migrated_metadata.tables["move_job"].insert(),
                {
                    "id": job_id,
                    "title": "migration probe",
                    "status": "DRAFT",
                    "created_at": created_at,
                    "updated_at": created_at,
                },
            )
            connection.execute(
                migrated_metadata.tables["job_participant"].insert(),
                {
                    "id": participant_id,
                    "job_id": job_id,
                    "role": "CUSTOMER",
                    "display_name": "migration probe",
                    "created_at": created_at,
                },
            )
            connection.execute(
                migrated_metadata.tables["capture_session"].insert(),
                {
                    "id": capture_id,
                    "job_id": job_id,
                    "created_by_participant_id": participant_id,
                    "created_at": created_at,
                },
            )
            connection.execute(
                migrated_metadata.tables["scope_version"].insert(),
                {
                    "id": scope_version_id,
                    "job_id": job_id,
                    "sequence_number": 1,
                    "content": {"schema_version": 1, "items": []},
                    "content_hash": "a" * 64,
                    "source_analysis_run_id": uuid4().hex,
                    "source_capture_session_id": capture_id,
                    "analysis_source": {"result_schema_version": 1},
                    "created_by_participant_id": None,
                    "created_at": created_at,
                },
            )

        command.downgrade(configuration, ALEMBIC_PREVIOUS)
        scope_columns = {column["name"] for column in inspect(engine).get_columns("scope_version")}
        assert "source_analysis_run_id" not in scope_columns
        assert "source_capture_session_id" not in scope_columns
        assert "analysis_source" not in scope_columns
        assert "scope_approval" in inspect(engine).get_table_names()
        assert "scope_version" in inspect(engine).get_table_names()
        assert "capture_session" in inspect(engine).get_table_names()
        assert "media_asset" in inspect(engine).get_table_names()
        assert "participant_access_token" in inspect(engine).get_table_names()
        assert "move_job" in inspect(engine).get_table_names()
        assert "existing_schema_probe" in inspect(engine).get_table_names()
        with engine.connect() as connection:
            restored_creator = connection.scalar(
                select(migrated_metadata.tables["scope_version"].c.created_by_participant_id).where(
                    migrated_metadata.tables["scope_version"].c.id == scope_version_id
                )
            )
        assert restored_creator == participant_id

        command.downgrade(configuration, ALEMBIC_BASELINE)
        assert BUSINESS_TABLES.isdisjoint(inspect(engine).get_table_names())
        assert "existing_schema_probe" in inspect(engine).get_table_names()

        command.upgrade(configuration, "head")
        assert _current_revision(engine) == ALEMBIC_HEAD
    finally:
        probe_metadata.drop_all(engine)
        engine.dispose()
