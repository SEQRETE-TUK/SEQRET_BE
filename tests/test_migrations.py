"""Fast migration graph and SQLite compatibility tests."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_BASELINE = "fnd_a02_0001"
ALEMBIC_ANALYSIS_PREVIOUS = "a_05_0001"
ALEMBIC_CHANGE_PREVIOUS = "a_06_0001"
ALEMBIC_PREVIOUS = "a_07_0001"
ALEMBIC_MAIN_HEAD = "a_09_0002"
ALEMBIC_HEAD = "a_09_0003"
ALEMBIC_OPERATIONAL_EVENT_PREVIOUS = "b_03_0001"
ALEMBIC_OUTBOX_PREVIOUS = "a_08_0001"
ALEMBIC_RATE_LIMIT_PREVIOUS = "a_09_0001"
ALEMBIC_BACKGROUND_JOB_PREVIOUS = "a_10_0001"
BUSINESS_TABLES = {
    "ai_analysis_run",
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
    "detection",
    "audit_event",
    "outbox_event",
    "event_consumption",
    "notification_delivery",
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


def test_operational_event_rows_block_schema_downgrade(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'event-guard.sqlite3').as_posix()}"
    configuration = _alembic_config(database_url)
    engine = create_engine(database_url)
    metadata = MetaData()

    try:
        command.upgrade(configuration, "head")
        metadata.reflect(
            engine,
            only=(
                "event_consumption",
                "job_participant",
                "move_job",
                "notification_delivery",
                "outbox_event",
            ),
        )
        created_at = datetime.now(UTC)
        job_id = uuid4().hex
        participant_id = uuid4().hex
        with engine.begin() as connection:
            connection.execute(
                metadata.tables["move_job"].insert(),
                {
                    "id": job_id,
                    "title": "event guard",
                    "status": "DRAFT",
                    "created_at": created_at,
                    "updated_at": created_at,
                },
            )
            connection.execute(
                metadata.tables["job_participant"].insert(),
                {
                    "id": participant_id,
                    "job_id": job_id,
                    "role": "CUSTOMER",
                    "display_name": "event guard",
                    "created_at": created_at,
                },
            )

        event_id = uuid4().hex
        guarded_rows = (
            (
                metadata.tables["outbox_event"],
                {
                    "event_id": event_id,
                    "event_type": "SCOPE_LOCKED_V1",
                    "schema_version": 1,
                    "aggregate_id": job_id,
                    "trace_id": "0" * 32,
                    "payload": {},
                    "occurred_at": created_at,
                    "next_attempt_at": created_at,
                },
            ),
            (
                metadata.tables["notification_delivery"],
                {
                    "id": uuid4().hex,
                    "event_id": event_id,
                    "event_type": "SCOPE_LOCKED_V1",
                    "job_id": job_id,
                    "recipient_participant_id": participant_id,
                    "status": "PENDING",
                    "attempt_count": 0,
                    "created_at": created_at,
                },
            ),
            (
                metadata.tables["event_consumption"],
                {
                    "consumer_name": "event-guard",
                    "event_id": event_id,
                    "consumed_at": created_at,
                },
            ),
        )
        for table, values in guarded_rows:
            with engine.begin() as connection:
                connection.execute(table.insert(), values)
            with pytest.raises(RuntimeError, match="roll back the application"):
                command.downgrade(configuration, ALEMBIC_OPERATIONAL_EVENT_PREVIOUS)
            assert _current_revision(engine) == ALEMBIC_HEAD
            with engine.begin() as connection:
                assert connection.scalar(select(table.c[next(iter(values))])) is not None
                connection.execute(table.delete())

        command.downgrade(configuration, ALEMBIC_OPERATIONAL_EVENT_PREVIOUS)
        assert _current_revision(engine) == ALEMBIC_OPERATIONAL_EVENT_PREVIOUS
        command.upgrade(configuration, "head")
        assert _current_revision(engine) == ALEMBIC_HEAD
    finally:
        engine.dispose()


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
        background_checks = {
            check["name"] for check in inspect(engine).get_check_constraints("background_job")
        }
        assert {
            "background_job_dispatch_lease",
            "background_job_execution_deadline",
            "background_job_generation_present",
            "background_job_status",
        } <= background_checks
        background_indexes = {
            index["name"] for index in inspect(engine).get_indexes("background_job")
        }
        assert {
            "ix_background_job_dispatch",
            "ix_background_job_dispatch_lease",
            "ix_background_job_move_job_created",
        } <= background_indexes
        assert "media_asset_metadata_state" in {
            check["name"] for check in inspect(engine).get_check_constraints("media_asset")
        }
        assert {"outbox_payload_object", "outbox_schema_version_one"} <= {
            check["name"] for check in inspect(engine).get_check_constraints("outbox_event")
        }

        command.downgrade(configuration, "base")
        assert _current_revision(engine) is None

        command.upgrade(configuration, ALEMBIC_MAIN_HEAD)
        probe_metadata.create_all(engine)
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

        migrated_metadata = MetaData()
        migrated_metadata.reflect(
            engine,
            only=(
                "move_job",
                "job_participant",
                "location",
                "room_zone",
                "capture_session",
                "media_asset",
                "outbox_event",
                "scope_version",
                "background_job",
            ),
        )
        job_id = uuid4().hex
        participant_id = uuid4().hex
        capture_id = uuid4().hex
        location_id = uuid4().hex
        room_zone_id = uuid4().hex
        media_asset_id = uuid4().hex
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
                migrated_metadata.tables["location"].insert(),
                {
                    "id": location_id,
                    "job_id": job_id,
                    "kind": "ORIGIN",
                    "label": "migration probe",
                    "created_at": created_at,
                },
            )
            connection.execute(
                migrated_metadata.tables["room_zone"].insert(),
                {
                    "id": room_zone_id,
                    "location_id": location_id,
                    "name": "migration probe",
                    "sort_order": 0,
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
                migrated_metadata.tables["media_asset"].insert(),
                {
                    "id": media_asset_id,
                    "capture_session_id": capture_id,
                    "room_zone_id": room_zone_id,
                    "media_purpose": "COMPLETION",
                    "status": "UPLOADED",
                    "object_key": f"migration/{media_asset_id}",
                    "content_type": "image/jpeg",
                    "expected_size_bytes": 10,
                    "actual_size_bytes": 10,
                    "generation": "7",
                    "created_at": created_at,
                    "uploaded_at": created_at,
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

        outbox_event = {
            "event_id": uuid4().hex,
            "event_type": "SCOPE_LOCKED_V1",
            "schema_version": 1,
            "aggregate_id": job_id,
            "trace_id": "0" * 32,
            "payload": {},
            "occurred_at": created_at,
            "next_attempt_at": created_at,
        }
        invalid_outbox_events = (
            outbox_event | {"event_id": uuid4().hex, "schema_version": 2},
            outbox_event | {"event_id": uuid4().hex, "payload": []},
        )
        for invalid_outbox_event in invalid_outbox_events:
            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.execute(
                    migrated_metadata.tables["outbox_event"].insert(),
                    invalid_outbox_event,
                )
        with engine.begin() as connection:
            connection.execute(
                migrated_metadata.tables["outbox_event"].insert(),
                outbox_event,
            )
            connection.execute(migrated_metadata.tables["outbox_event"].delete())

        command.downgrade(configuration, "a_12_0001")
        assert _current_revision(engine) == "a_12_0001"
        command.upgrade(configuration, "head")
        assert _current_revision(engine) == ALEMBIC_HEAD
        with engine.connect() as connection:
            preserved_generation = connection.scalar(
                select(migrated_metadata.tables["media_asset"].c.generation).where(
                    migrated_metadata.tables["media_asset"].c.id == media_asset_id
                )
            )
        assert preserved_generation == "7"

        invalid_media_assets = (
            {
                "status": "PENDING_UPLOAD",
                "generation": "unexpected",
            },
            {
                "status": "UPLOADED",
                "actual_size_bytes": 10,
                "uploaded_at": created_at,
            },
        )
        for invalid_state in invalid_media_assets:
            invalid_media_asset = {
                "id": uuid4().hex,
                "capture_session_id": capture_id,
                "room_zone_id": room_zone_id,
                "media_purpose": "COMPLETION",
                "object_key": f"migration/{uuid4().hex}",
                "content_type": "image/jpeg",
                "expected_size_bytes": 10,
                "created_at": created_at,
            } | invalid_state
            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.execute(
                    migrated_metadata.tables["media_asset"].insert(),
                    invalid_media_asset,
                )

        invalid_background_job = {
            "id": uuid4().hex,
            "move_job_id": job_id,
            "media_asset_id": media_asset_id,
            "job_type": "MEDIA_RETENTION_DELETE",
            "status": "PENDING",
            "target_object_key": f"migration/{media_asset_id}",
            "target_generation": "7",
            "trace_id": "0" * 32,
            "scheduled_at": created_at,
            "attempt_count": 0,
            "dispatch_token": uuid4().hex,
            "created_at": created_at,
        }
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                migrated_metadata.tables["background_job"].insert(),
                invalid_background_job,
            )

        valid_background_job = invalid_background_job | {"dispatch_token": None}
        with engine.begin() as connection:
            connection.execute(
                migrated_metadata.tables["background_job"].insert(),
                valid_background_job,
            )
        with pytest.raises(RuntimeError, match="roll back the application"):
            command.downgrade(configuration, ALEMBIC_BACKGROUND_JOB_PREVIOUS)
        assert _current_revision(engine) == ALEMBIC_OPERATIONAL_EVENT_PREVIOUS
        with engine.begin() as connection:
            connection.execute(migrated_metadata.tables["background_job"].delete())

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
        with engine.connect() as connection:
            ai_creator = connection.scalar(
                select(migrated_metadata.tables["scope_version"].c.created_by_participant_id).where(
                    migrated_metadata.tables["scope_version"].c.id == scope_version_id
                )
            )
        assert ai_creator is None

        command.downgrade(configuration, ALEMBIC_ANALYSIS_PREVIOUS)
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
