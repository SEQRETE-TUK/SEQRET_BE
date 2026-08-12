"""Fast migration graph and SQLite compatibility tests."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect, text
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_BASELINE = "fnd_a02_0001"
ALEMBIC_PREVIOUS = "a_04_0001"
ALEMBIC_HEAD = "a_05_0001"
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

        command.downgrade(configuration, ALEMBIC_PREVIOUS)
        assert "scope_approval" not in inspect(engine).get_table_names()
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
        probe_metadata.drop_all(engine)
        engine.dispose()
