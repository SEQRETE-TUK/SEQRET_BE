"""Fast migration graph and SQLite compatibility tests."""

from datetime import UTC, datetime, timedelta
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
ALEMBIC_HEAD = "a_23_0001"
ALEMBIC_AI_V2_HEAD = "b_08_0001"
ALEMBIC_AI_V2_PREVIOUS = "a_19_0001"
ALEMBIC_MEDIA_CONSENT_PREVIOUS = "a_16_0001"
ALEMBIC_LOCATION_PREVIOUS = "int_04_0001"
ALEMBIC_COMPLETION_PREVIOUS = "a_13_0001"
ALEMBIC_DISPATCH_PREVIOUS = "int_03_0002"
ALEMBIC_FIELD_CHANGE_PREVIOUS = "int_02_0001"
ALEMBIC_SCOPE_REVIEW_PREVIOUS = "a_02_0002"
ALEMBIC_INVITATION_PREVIOUS = "int_01_0001"
ALEMBIC_CAPTURE_ANALYSIS_PREVIOUS = "int_03_0001"
ALEMBIC_MEDIA_VALIDATION_PREVIOUS = "a_08_0002"
ALEMBIC_AUDIT_PREVIOUS = "a_09_0003"
ALEMBIC_OPERATIONAL_EVENT_PREVIOUS = "b_03_0001"
ALEMBIC_OUTBOX_PREVIOUS = "a_08_0001"
ALEMBIC_RATE_LIMIT_PREVIOUS = "a_09_0001"
ALEMBIC_BACKGROUND_JOB_PREVIOUS = "a_10_0001"
BUSINESS_TABLES = {
    "ai_analysis_run",
    "background_job",
    "capture_analysis_dispatch",
    "capture_session",
    "job_participant",
    "location",
    "media_asset",
    "move_job",
    "participant_access_token",
    "participant_invitation",
    "room_zone",
    "scope_version",
    "scope_approval",
    "scope_proposal",
    "scope_revision_request",
    "field_issue",
    "field_issue_evidence",
    "change_proposal_detail",
    "dispatch_setup",
    "dispatch_plan",
    "field_check_in",
    "completion_submission",
    "completion_submission_evidence",
    "completion_request",
    "completion_problem_report",
    "change_request",
    "change_request_evidence",
    "completion_confirmation",
    "completion_evidence",
    "detection",
    "analysis_location_condition_suggestion",
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


def test_scope_execution_plan_migration_roundtrips_and_guards_history(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'scope-plan.sqlite3').as_posix()}"
    configuration = _alembic_config(database_url)
    engine = create_engine(database_url)

    try:
        command.upgrade(configuration, ALEMBIC_AI_V2_HEAD)
        assert "execution_plan" not in {
            column["name"] for column in inspect(engine).get_columns("scope_proposal")
        }

        command.upgrade(configuration, "head")
        proposal = Table("scope_proposal", MetaData(), autoload_with=engine)
        now = datetime.now(UTC)
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                proposal.insert(),
                {
                    "id": uuid4().hex,
                    "job_id": uuid4().hex,
                    "source_scope_version_id": uuid4().hex,
                    "result_scope_version_id": uuid4().hex,
                    "proposed_by_participant_id": uuid4().hex,
                    "kind": "INITIAL",
                    "status": "CUSTOMER_REVIEW",
                    "base_amount_krw": 500_000,
                    "adjustments": [],
                    "total_amount_krw": 500_000,
                    "execution_plan": {
                        "vehicle_count": 1,
                        "vehicle_description": "1톤 탑차",
                        "worker_count": 2,
                        "estimated_duration_minutes": 180,
                        "notes": None,
                    },
                    "included_works": ["운반"],
                    "exclusions": [],
                    "reason": "migration guard",
                    "sent_at": now,
                },
            )

        with pytest.raises(RuntimeError, match="scope execution plan rows exist"):
            command.downgrade(configuration, ALEMBIC_AI_V2_HEAD)
        assert _current_revision(engine) == ALEMBIC_HEAD

        with engine.begin() as connection:
            connection.execute(proposal.delete())
        command.downgrade(configuration, ALEMBIC_AI_V2_HEAD)
        assert "execution_plan" not in {
            column["name"] for column in inspect(engine).get_columns("scope_proposal")
        }
        command.upgrade(configuration, "head")
        assert _current_revision(engine) == ALEMBIC_HEAD
    finally:
        engine.dispose()


def test_ai_v2_migration_backfills_v1_roundtrips_and_guards_v2_history(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'ai-v2.sqlite3').as_posix()}"
    configuration = _alembic_config(database_url)
    engine = create_engine(database_url)

    try:
        command.upgrade(configuration, ALEMBIC_AI_V2_PREVIOUS)
        metadata = MetaData()
        metadata.reflect(
            engine,
            only=("move_job", "job_participant", "capture_session", "ai_analysis_run", "detection"),
        )
        now = datetime.now(UTC)
        job_id = uuid4().hex
        participant_id = uuid4().hex
        capture_id = uuid4().hex
        v1_run_id = uuid4().hex
        with engine.begin() as connection:
            connection.execute(
                metadata.tables["move_job"].insert(),
                {
                    "id": job_id,
                    "title": "AI v2 migration",
                    "status": "DRAFT",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                metadata.tables["job_participant"].insert(),
                {
                    "id": participant_id,
                    "job_id": job_id,
                    "role": "CUSTOMER",
                    "display_name": "customer",
                    "created_at": now,
                },
            )
            connection.execute(
                metadata.tables["capture_session"].insert(),
                {
                    "id": capture_id,
                    "job_id": job_id,
                    "created_by_participant_id": participant_id,
                    "created_at": now,
                },
            )
            connection.execute(
                metadata.tables["ai_analysis_run"].insert(),
                {
                    "id": v1_run_id,
                    "capture_session_id": capture_id,
                    "status": "COMPLETED",
                    "attempt_count": 1,
                    "trace_id": "a" * 32,
                    "model_name": "fake",
                    "model_version": "v1",
                    "prompt_version": "inventory-1",
                    "result_schema_version": 1,
                    "started_at": now,
                    "completed_at": now,
                    "created_at": now,
                },
            )
            connection.execute(
                metadata.tables["detection"].insert(),
                {
                    "id": uuid4().hex,
                    "analysis_run_id": v1_run_id,
                    "ordinal": 0,
                    "item_key": "bed",
                    "description": "침대",
                    "confidence": 0.8,
                    "review_required": False,
                    "source_media_asset_ids": [],
                    "created_at": now,
                },
            )

        command.upgrade(configuration, "head")
        migrated = MetaData()
        migrated.reflect(
            engine,
            only=(
                "ai_analysis_run",
                "detection",
                "analysis_location_condition_suggestion",
            ),
        )
        detection = migrated.tables["detection"]
        with engine.begin() as connection:
            legacy = connection.execute(select(detection)).mappings().one()
        assert legacy["item_schema_version"] == 1
        assert legacy["name"] is None

        command.downgrade(configuration, ALEMBIC_AI_V2_PREVIOUS)
        columns = {column["name"] for column in inspect(engine).get_columns("detection")}
        assert "item_schema_version" not in columns
        assert "analysis_location_condition_suggestion" not in inspect(engine).get_table_names()

        command.upgrade(configuration, "head")
        migrated = MetaData()
        migrated.reflect(
            engine,
            only=(
                "ai_analysis_run",
                "detection",
                "analysis_location_condition_suggestion",
            ),
        )
        v2_run_id = uuid4().hex
        location_id = uuid4().hex
        source_id = uuid4().hex
        with engine.begin() as connection:
            connection.execute(
                migrated.tables["ai_analysis_run"].insert(),
                {
                    "id": v2_run_id,
                    "capture_session_id": capture_id,
                    "status": "COMPLETED",
                    "attempt_count": 1,
                    "trace_id": "b" * 32,
                    "model_name": "fake",
                    "model_version": "v2",
                    "prompt_version": "inventory-1",
                    "result_schema_version": 2,
                    "started_at": now,
                    "completed_at": now,
                    "created_at": now,
                },
            )
            connection.execute(
                migrated.tables["detection"].insert(),
                {
                    "id": uuid4().hex,
                    "analysis_run_id": v2_run_id,
                    "ordinal": 0,
                    "item_schema_version": 2,
                    "item_key": "fridge",
                    "description": "냉장고 1대",
                    "name": "냉장고",
                    "quantity": 1,
                    "unit": "대",
                    "confidence": 0.9,
                    "review_required": False,
                    "source_media_asset_ids": [source_id],
                    "created_at": now,
                },
            )
            connection.execute(
                migrated.tables["analysis_location_condition_suggestion"].insert(),
                {
                    "id": uuid4().hex,
                    "analysis_run_id": v2_run_id,
                    "ordinal": 0,
                    "location_id": location_id,
                    "location_kind": "origin",
                    "residence_type": "apartment",
                    "floor_status": "known",
                    "floor_value": 3,
                    "elevator": "available",
                    "stairs": "not_required",
                    "parking_access": "restricted",
                    "carry_distance_status": "known",
                    "carry_distance_value_m": 25,
                    "confidence": 0.8,
                    "review_required_fields": ["parking_access"],
                    "source_media_asset_ids": [source_id],
                    "created_at": now,
                },
            )
        with pytest.raises(RuntimeError, match="AI result v2 rows exist"):
            command.downgrade(configuration, ALEMBIC_AI_V2_PREVIOUS)
        assert _current_revision(engine) == ALEMBIC_AI_V2_HEAD
    finally:
        engine.dispose()


def test_location_conditions_migration_backfills_and_defaults_existing_clients(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'location-conditions.sqlite3').as_posix()}"
    configuration = _alembic_config(database_url)
    engine = create_engine(database_url)

    try:
        command.upgrade(configuration, ALEMBIC_LOCATION_PREVIOUS)
        metadata = MetaData()
        metadata.reflect(engine, only=("move_job", "location"))
        now = datetime.now(UTC)
        job_id = uuid4().hex
        origin_id = uuid4().hex
        with engine.begin() as connection:
            connection.execute(
                metadata.tables["move_job"].insert(),
                {
                    "id": job_id,
                    "title": "location conditions backfill",
                    "status": "DRAFT",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                metadata.tables["location"].insert(),
                {
                    "id": origin_id,
                    "job_id": job_id,
                    "kind": "ORIGIN",
                    "label": "출발지",
                    "created_at": now,
                },
            )

        command.upgrade(configuration, "head")
        migrated = MetaData()
        migrated.reflect(engine, only=("location",))
        with engine.begin() as connection:
            backfilled = connection.scalar(
                select(migrated.tables["location"].c.conditions).where(
                    migrated.tables["location"].c.id == origin_id
                )
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                migrated.tables["location"].insert(),
                {
                    "id": uuid4().hex,
                    "job_id": job_id,
                    "kind": "DESTINATION",
                    "label": "도착지",
                    "created_at": now,
                },
            )
        expected = {
            "residence_type": "unknown",
            "floor": {"status": "unknown", "value": None},
            "elevator": "unknown",
            "stairs": "unknown",
            "parking_access": "unknown",
            "carry_distance": {"status": "unknown", "value_m": None},
            "access_note": None,
        }
        assert backfilled == expected

        command.downgrade(configuration, ALEMBIC_LOCATION_PREVIOUS)
        assert "conditions" not in {
            column["name"] for column in inspect(engine).get_columns("location")
        }
    finally:
        engine.dispose()


def test_media_consent_migration_marks_legacy_and_enforces_complete_snapshot(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'media-consent.sqlite3').as_posix()}"
    configuration = _alembic_config(database_url)
    engine = create_engine(database_url)

    try:
        command.upgrade(configuration, ALEMBIC_MEDIA_CONSENT_PREVIOUS)
        metadata = MetaData()
        metadata.reflect(engine, only=("move_job", "job_participant", "capture_session"))
        now = datetime.now(UTC)
        job_id = uuid4().hex
        participant_id = uuid4().hex
        capture_id = uuid4().hex
        with engine.begin() as connection:
            connection.execute(
                metadata.tables["move_job"].insert(),
                {
                    "id": job_id,
                    "title": "media consent backfill",
                    "status": "DRAFT",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                metadata.tables["job_participant"].insert(),
                {
                    "id": participant_id,
                    "job_id": job_id,
                    "role": "CUSTOMER",
                    "display_name": "customer",
                    "created_at": now,
                },
            )
            connection.execute(
                metadata.tables["capture_session"].insert(),
                {
                    "id": capture_id,
                    "job_id": job_id,
                    "created_by_participant_id": participant_id,
                    "created_at": now,
                },
            )

        command.upgrade(configuration, "head")
        migrated = MetaData()
        migrated.reflect(engine, only=("capture_session",))
        table = migrated.tables["capture_session"]
        with engine.begin() as connection:
            legacy = (
                connection.execute(select(table).where(table.c.id == capture_id)).mappings().one()
            )
        assert legacy["privacy_notice_acknowledged"] is False
        assert legacy["media_consent_policy_version"] is None
        assert legacy["media_retention_days"] is None
        assert legacy["media_consented_at"] is None

        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                table.insert(),
                {
                    "id": uuid4().hex,
                    "job_id": job_id,
                    "created_by_participant_id": participant_id,
                    "privacy_notice_acknowledged": True,
                    "media_consent_policy_version": "2026-08-17.v1",
                    "media_retention_days": None,
                    "media_consented_at": now,
                    "created_at": now,
                },
            )
        with engine.begin() as connection:
            omitted_acknowledgement_id = uuid4().hex
            connection.execute(
                table.insert(),
                {
                    "id": omitted_acknowledgement_id,
                    "job_id": job_id,
                    "created_by_participant_id": participant_id,
                    "created_at": now,
                },
            )
            connection.execute(
                table.insert(),
                {
                    "id": uuid4().hex,
                    "job_id": job_id,
                    "created_by_participant_id": participant_id,
                    "privacy_notice_acknowledged": True,
                    "media_consent_policy_version": "2026-08-17.v1",
                    "media_retention_days": 30,
                    "media_consented_at": now,
                    "created_at": now,
                },
            )
            omitted_acknowledgement = (
                connection.execute(select(table).where(table.c.id == omitted_acknowledgement_id))
                .mappings()
                .one()
            )
        assert omitted_acknowledgement["privacy_notice_acknowledged"] is False
        assert omitted_acknowledgement["media_consent_policy_version"] is None
        assert omitted_acknowledgement["media_retention_days"] is None
        assert omitted_acknowledgement["media_consented_at"] is None

        command.downgrade(configuration, ALEMBIC_MEDIA_CONSENT_PREVIOUS)
        columns = {column["name"] for column in inspect(engine).get_columns("capture_session")}
        assert "media_consent_policy_version" not in columns
        assert "privacy_notice_acknowledged" not in columns
    finally:
        engine.dispose()


def test_invitation_history_blocks_schema_downgrade(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'invitation-guard.sqlite3').as_posix()}"
    configuration = _alembic_config(database_url)
    engine = create_engine(database_url)
    metadata = MetaData()

    try:
        command.upgrade(configuration, "head")
        metadata.reflect(
            engine,
            only=(
                "move_job",
                "job_participant",
                "participant_access_token",
                "participant_invitation",
            ),
        )
        now = datetime.now(UTC)
        job_id = uuid4().hex
        customer_id = uuid4().hex
        manager_id = uuid4().hex
        access_link_id = uuid4().hex
        customer_access_link_id = uuid4().hex
        invitation_id = uuid4().hex
        with engine.begin() as connection:
            connection.execute(
                metadata.tables["move_job"].insert(),
                {
                    "id": job_id,
                    "title": "invitation guard",
                    "status": "DRAFT",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                metadata.tables["job_participant"].insert(),
                [
                    {
                        "id": customer_id,
                        "job_id": job_id,
                        "role": "CUSTOMER",
                        "display_name": "customer",
                        "created_at": now,
                    },
                    {
                        "id": manager_id,
                        "job_id": job_id,
                        "role": "COMPANY_MANAGER",
                        "display_name": "manager",
                        "created_at": now,
                    },
                ],
            )
            connection.execute(
                metadata.tables["participant_access_token"].insert(),
                [
                    {
                        "id": access_link_id,
                        "participant_id": manager_id,
                        "token_hash": "a" * 64,
                        "expires_at": now + timedelta(days=7),
                        "created_at": now,
                    },
                    {
                        "id": customer_access_link_id,
                        "participant_id": customer_id,
                        "token_hash": "b" * 64,
                        "expires_at": now + timedelta(days=7),
                        "created_at": now,
                    },
                ],
            )
            connection.execute(
                metadata.tables["participant_invitation"].insert(),
                {
                    "id": invitation_id,
                    "job_id": job_id,
                    "issuer_participant_id": customer_id,
                    "invitee_participant_id": manager_id,
                    "access_link_id": access_link_id,
                    "role": "COMPANY_MANAGER",
                    "status": "PENDING",
                    "issued_at": now,
                    "expires_at": now + timedelta(days=7),
                    "created_at": now,
                },
            )

        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                metadata.tables["participant_invitation"].insert(),
                {
                    "id": uuid4().hex,
                    "job_id": job_id,
                    "issuer_participant_id": manager_id,
                    "invitee_participant_id": customer_id,
                    "access_link_id": customer_access_link_id,
                    "role": "CUSTOMER",
                    "status": "PENDING",
                    "issued_at": now,
                    "expires_at": now + timedelta(days=7),
                    "created_at": now,
                },
            )
        with pytest.raises(RuntimeError, match="roll back the application"):
            command.downgrade(configuration, "int_01_0001")
        assert _current_revision(engine) == ALEMBIC_SCOPE_REVIEW_PREVIOUS

        with engine.begin() as connection:
            connection.execute(metadata.tables["participant_invitation"].delete())
        command.downgrade(configuration, "int_01_0001")
        assert "participant_invitation" not in inspect(engine).get_table_names()
        command.upgrade(configuration, "head")
    finally:
        engine.dispose()


def test_scope_review_history_blocks_schema_downgrade(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'scope-review-guard.sqlite3').as_posix()}"
    configuration = _alembic_config(database_url)
    engine = create_engine(database_url)
    metadata = MetaData()

    try:
        command.upgrade(configuration, "head")
        metadata.reflect(
            engine,
            only=(
                "move_job",
                "job_participant",
                "scope_version",
                "scope_proposal",
            ),
        )
        now = datetime.now(UTC)
        job_id = uuid4().hex
        customer_id = uuid4().hex
        manager_id = uuid4().hex
        source_scope_id = uuid4().hex
        result_scope_id = uuid4().hex
        proposal_id = uuid4().hex
        with engine.begin() as connection:
            connection.execute(
                metadata.tables["move_job"].insert(),
                {
                    "id": job_id,
                    "title": "scope review guard",
                    "status": "DRAFT",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                metadata.tables["job_participant"].insert(),
                [
                    {
                        "id": customer_id,
                        "job_id": job_id,
                        "role": "CUSTOMER",
                        "display_name": "customer",
                        "created_at": now,
                    },
                    {
                        "id": manager_id,
                        "job_id": job_id,
                        "role": "COMPANY_MANAGER",
                        "display_name": "manager",
                        "created_at": now,
                    },
                ],
            )
            connection.execute(
                metadata.tables["scope_version"].insert(),
                {
                    "id": source_scope_id,
                    "job_id": job_id,
                    "sequence_number": 1,
                    "content": {"schema_version": 1, "items": []},
                    "content_hash": "a" * 64,
                    "created_by_participant_id": customer_id,
                    "created_at": now,
                },
            )
            connection.execute(
                metadata.tables["scope_version"].insert(),
                {
                    "id": result_scope_id,
                    "job_id": job_id,
                    "parent_version_id": source_scope_id,
                    "sequence_number": 2,
                    "content": {"schema_version": 1, "items": []},
                    "content_hash": "b" * 64,
                    "created_by_participant_id": manager_id,
                    "created_at": now,
                },
            )
            connection.execute(
                metadata.tables["scope_proposal"].insert(),
                {
                    "id": proposal_id,
                    "job_id": job_id,
                    "source_scope_version_id": source_scope_id,
                    "result_scope_version_id": result_scope_id,
                    "proposed_by_participant_id": manager_id,
                    "kind": "INITIAL",
                    "status": "CUSTOMER_REVIEW",
                    "base_amount_krw": 100_000,
                    "adjustments": [],
                    "total_amount_krw": 100_000,
                    "included_works": [],
                    "exclusions": [],
                    "reason": "initial quote",
                    "sent_at": now,
                },
            )

        with pytest.raises(RuntimeError, match="roll back the application"):
            command.downgrade(configuration, ALEMBIC_SCOPE_REVIEW_PREVIOUS)
        assert _current_revision(engine) == ALEMBIC_FIELD_CHANGE_PREVIOUS

        with engine.begin() as connection:
            connection.execute(metadata.tables["scope_proposal"].delete())
        command.downgrade(configuration, ALEMBIC_SCOPE_REVIEW_PREVIOUS)
        assert "scope_proposal" not in inspect(engine).get_table_names()
        assert "scope_revision_request" not in inspect(engine).get_table_names()
        command.upgrade(configuration, "head")
    finally:
        engine.dispose()


def test_field_change_history_blocks_schema_downgrade(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'field-change-guard.sqlite3').as_posix()}"
    configuration = _alembic_config(database_url)
    engine = create_engine(database_url)
    metadata = MetaData()

    try:
        command.upgrade(configuration, "head")
        metadata.reflect(
            engine,
            only=(
                "move_job",
                "job_participant",
                "scope_version",
                "field_issue",
            ),
        )
        now = datetime.now(UTC)
        job_id = uuid4().hex
        worker_id = uuid4().hex
        scope_id = uuid4().hex
        issue_id = uuid4().hex
        with engine.begin() as connection:
            connection.execute(
                metadata.tables["move_job"].insert(),
                {
                    "id": job_id,
                    "title": "field change guard",
                    "status": "DRAFT",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                metadata.tables["job_participant"].insert(),
                {
                    "id": worker_id,
                    "job_id": job_id,
                    "role": "FIELD_WORKER",
                    "display_name": "worker",
                    "created_at": now,
                },
            )
            connection.execute(
                metadata.tables["scope_version"].insert(),
                {
                    "id": scope_id,
                    "job_id": job_id,
                    "sequence_number": 1,
                    "content": {"schema_version": 1, "items": []},
                    "content_hash": "c" * 64,
                    "created_by_participant_id": worker_id,
                    "created_at": now,
                    "locked_at": now,
                },
            )
            connection.execute(
                metadata.tables["field_issue"].insert(),
                {
                    "id": issue_id,
                    "job_id": job_id,
                    "client_reference": uuid4().hex,
                    "base_scope_version_id": scope_id,
                    "reported_by_participant_id": worker_id,
                    "issue_type": "SITE_BLOCKER",
                    "title": "site blocker",
                    "description": "synthetic evidence",
                    "created_at": now,
                },
            )

        with pytest.raises(RuntimeError, match="roll back the application"):
            command.downgrade(configuration, ALEMBIC_FIELD_CHANGE_PREVIOUS)
        assert _current_revision(engine) == ALEMBIC_DISPATCH_PREVIOUS

        with engine.begin() as connection:
            connection.execute(metadata.tables["field_issue"].delete())
        command.downgrade(configuration, ALEMBIC_FIELD_CHANGE_PREVIOUS)
        assert "field_issue" not in inspect(engine).get_table_names()
        assert "field_issue_evidence" not in inspect(engine).get_table_names()
        assert "change_proposal_detail" not in inspect(engine).get_table_names()
        command.upgrade(configuration, "head")
    finally:
        engine.dispose()


def test_dispatch_history_blocks_schema_downgrade(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'dispatch-guard.sqlite3').as_posix()}"
    configuration = _alembic_config(database_url)
    engine = create_engine(database_url)
    metadata = MetaData()

    try:
        command.upgrade(configuration, "head")
        metadata.reflect(
            engine,
            only=(
                "move_job",
                "job_participant",
                "scope_version",
                "dispatch_setup",
                "outbox_event",
                "notification_delivery",
            ),
        )
        now = datetime.now(UTC)
        job_id = uuid4().hex
        manager_id = uuid4().hex
        worker_id = uuid4().hex
        scope_id = uuid4().hex
        setup_id = uuid4().hex
        event_id = uuid4().hex
        completion_event_id = uuid4().hex
        with engine.begin() as connection:
            connection.execute(
                metadata.tables["move_job"].insert(),
                {
                    "id": job_id,
                    "title": "dispatch guard",
                    "status": "DRAFT",
                    "scheduled_at": now,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                metadata.tables["job_participant"].insert(),
                [
                    {
                        "id": manager_id,
                        "job_id": job_id,
                        "role": "COMPANY_MANAGER",
                        "display_name": "manager",
                        "created_at": now,
                    },
                    {
                        "id": worker_id,
                        "job_id": job_id,
                        "role": "FIELD_WORKER",
                        "display_name": "worker",
                        "created_at": now,
                    },
                ],
            )
            connection.execute(
                metadata.tables["scope_version"].insert(),
                {
                    "id": scope_id,
                    "job_id": job_id,
                    "sequence_number": 1,
                    "content": {"schema_version": 1, "items": []},
                    "content_hash": "d" * 64,
                    "created_by_participant_id": manager_id,
                    "created_at": now,
                    "locked_at": now,
                },
            )
            connection.execute(
                metadata.tables["dispatch_setup"].insert(),
                {
                    "id": setup_id,
                    "job_id": job_id,
                    "client_reference": uuid4().hex,
                    "source_scope_version_id": scope_id,
                    "created_by_participant_id": manager_id,
                    "command_hash": "e" * 64,
                    "start_at": now,
                    "expected_duration_minutes": 60,
                    "required_vehicle_capacity_m2": 1,
                    "required_worker_count": 1,
                    "required_skills": [],
                    "required_certifications": [],
                    "check_in_items": [{"key": "safety", "label": "safety"}],
                    "completion_check_items": [
                        {"key": "tools_removed", "label": "작업 도구와 자재 회수"},
                        {"key": "site_restored", "label": "출발지와 도착지 정리"},
                        {"key": "changes_recorded", "label": "변경·이슈 기록 확인"},
                    ],
                    "origin_conditions": [],
                    "safety_notice": "safety",
                    "vehicle_options": [],
                    "worker_options": [],
                    "created_at": now,
                },
            )
            connection.execute(
                metadata.tables["outbox_event"].insert(),
                {
                    "event_id": event_id,
                    "event_type": "DISPATCH_CONFIRMED_V1",
                    "schema_version": 1,
                    "aggregate_id": job_id,
                    "actor_id": manager_id,
                    "trace_id": "0" * 32,
                    "payload": {
                        "dispatch_id": uuid4().hex,
                        "scope_version_id": scope_id,
                        "field_worker_participant_id": worker_id,
                    },
                    "occurred_at": now,
                    "next_attempt_at": now,
                },
            )
            connection.execute(
                metadata.tables["notification_delivery"].insert(),
                {
                    "id": uuid4().hex,
                    "event_id": event_id,
                    "event_type": "DISPATCH_CONFIRMED_V1",
                    "job_id": job_id,
                    "recipient_participant_id": worker_id,
                    "status": "PENDING",
                    "attempt_count": 0,
                    "created_at": now,
                },
            )
            connection.execute(
                metadata.tables["outbox_event"].insert(),
                {
                    "event_id": completion_event_id,
                    "event_type": "COMPLETION_SUBMITTED_V1",
                    "schema_version": 1,
                    "aggregate_id": job_id,
                    "actor_id": worker_id,
                    "trace_id": "1" * 32,
                    "payload": {
                        "completion_submission_id": uuid4().hex,
                        "scope_version_id": scope_id,
                        "field_worker_participant_id": worker_id,
                    },
                    "occurred_at": now,
                    "next_attempt_at": now,
                },
            )

        with pytest.raises(RuntimeError, match="INT-04 completion"):
            command.downgrade(configuration, ALEMBIC_COMPLETION_PREVIOUS)
        assert _current_revision(engine) == ALEMBIC_LOCATION_PREVIOUS
        with engine.begin() as connection:
            connection.execute(
                metadata.tables["outbox_event"]
                .delete()
                .where(metadata.tables["outbox_event"].c.event_id == completion_event_id)
            )
            connection.execute(
                metadata.tables["dispatch_setup"]
                .update()
                .values(
                    completion_check_items=[{"key": "custom_handoff", "label": "고객 맞춤 인계"}]
                )
            )
        with pytest.raises(RuntimeError, match="dispatch checklist history"):
            command.downgrade(configuration, ALEMBIC_COMPLETION_PREVIOUS)
        assert _current_revision(engine) == ALEMBIC_LOCATION_PREVIOUS
        with engine.begin() as connection:
            connection.execute(
                metadata.tables["dispatch_setup"]
                .update()
                .values(
                    completion_check_items=[
                        {"key": "tools_removed", "label": "작업 도구와 자재 회수"},
                        {"key": "site_restored", "label": "출발지와 도착지 정리"},
                        {"key": "changes_recorded", "label": "변경·이슈 기록 확인"},
                    ]
                )
            )

        with pytest.raises(RuntimeError, match="dispatch or check-in history"):
            command.downgrade(configuration, ALEMBIC_DISPATCH_PREVIOUS)
        assert _current_revision(engine) == ALEMBIC_COMPLETION_PREVIOUS

        with engine.begin() as connection:
            connection.execute(metadata.tables["notification_delivery"].delete())
            connection.execute(metadata.tables["outbox_event"].delete())
            connection.execute(metadata.tables["dispatch_setup"].delete())
        command.downgrade(configuration, ALEMBIC_DISPATCH_PREVIOUS)
        table_names = inspect(engine).get_table_names()
        assert "dispatch_setup" not in table_names
        assert "dispatch_plan" not in table_names
        assert "field_check_in" not in table_names
        command.upgrade(configuration, "head")
    finally:
        engine.dispose()


@pytest.mark.parametrize("history_table", ("audit_event", "completion_confirmation"))
def test_audit_invariants_on_sqlite(
    tmp_path: Path,
    history_table: str,
) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / f'{history_table}.sqlite3').as_posix()}"
    configuration = _alembic_config(database_url)
    engine = create_engine(database_url)
    metadata = MetaData()

    try:
        command.upgrade(configuration, "head")
        metadata.reflect(engine, only=(history_table,))
        created_at = datetime.now(UTC)
        table = metadata.tables[history_table]
        values = (
            {
                "id": uuid4().hex,
                "job_id": uuid4().hex,
                "event_type": "JOB_CREATED",
                "payload": {},
                "occurred_at": created_at,
            }
            if history_table == "audit_event"
            else {
                "id": uuid4().hex,
                "job_id": uuid4().hex,
                "scope_version_id": uuid4().hex,
                "participant_id": uuid4().hex,
                "role": "CUSTOMER",
                "confirmed_at": created_at,
            }
        )
        with engine.begin() as connection:
            connection.execute(table.insert(), values)

        if history_table == "audit_event":
            for mutation in (
                table.update().values(payload={"changed": True}),
                table.delete(),
            ):
                with (
                    pytest.raises(
                        IntegrityError,
                        match="append-only",
                    ),
                    engine.begin() as connection,
                ):
                    connection.execute(mutation)

        with pytest.raises(RuntimeError, match="roll back the application"):
            command.downgrade(configuration, ALEMBIC_AUDIT_PREVIOUS)
        assert _current_revision(engine) == ALEMBIC_MEDIA_VALIDATION_PREVIOUS
        command.upgrade(configuration, "head")
    finally:
        engine.dispose()


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
            assert _current_revision(engine) == ALEMBIC_AUDIT_PREVIOUS
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
            "background_job_target_shape",
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
        analysis_checks = {
            check["name"]
            for check in inspect(engine).get_check_constraints("capture_analysis_dispatch")
        }
        assert {
            "capture_analysis_dispatch_attempt_time",
            "capture_analysis_dispatch_completion_time",
            "capture_analysis_dispatch_failure",
            "capture_analysis_dispatch_lease",
            "capture_analysis_dispatch_scope_version",
            "capture_analysis_dispatch_status",
        } <= analysis_checks
        analysis_indexes = {
            index["name"] for index in inspect(engine).get_indexes("capture_analysis_dispatch")
        }
        assert {
            "ix_capture_analysis_dispatch_due",
            "ix_capture_analysis_dispatch_lease",
        } <= analysis_indexes

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
                "capture_analysis_dispatch",
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
                    "conditions": {
                        "residence_type": "unknown",
                        "floor": {"status": "unknown", "value": None},
                        "elevator": "unknown",
                        "stairs": "unknown",
                        "parking_access": "unknown",
                        "carry_distance": {"status": "unknown", "value_m": None},
                        "access_note": None,
                    },
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
                    "privacy_notice_acknowledged": False,
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
                "capture_analysis_dispatch",
            ),
        )

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
                "capture_analysis_dispatch",
            ),
        )
        capture_analysis_dispatch = {
            "analysis_run_id": uuid4().hex,
            "capture_session_id": capture_id,
            "move_job_id": job_id,
            "submitted_by_participant_id": participant_id,
            "status": "PENDING",
            "trace_id": "0" * 32,
            "scheduled_at": created_at,
            "dispatch_attempt_count": 0,
            "submitted_at": created_at,
        }
        invalid_capture_analysis = capture_analysis_dispatch | {
            "analysis_run_id": uuid4().hex,
            "dispatch_token": uuid4().hex,
        }
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                migrated_metadata.tables["capture_analysis_dispatch"].insert(),
                invalid_capture_analysis,
            )
        with engine.begin() as connection:
            connection.execute(
                migrated_metadata.tables["capture_analysis_dispatch"].insert(),
                capture_analysis_dispatch,
            )
        with pytest.raises(RuntimeError, match="roll back the application"):
            command.downgrade(configuration, ALEMBIC_CAPTURE_ANALYSIS_PREVIOUS)
        assert _current_revision(engine) == ALEMBIC_INVITATION_PREVIOUS
        with engine.begin() as connection:
            connection.execute(migrated_metadata.tables["capture_analysis_dispatch"].delete())
        command.downgrade(configuration, ALEMBIC_CAPTURE_ANALYSIS_PREVIOUS)
        assert _current_revision(engine) == ALEMBIC_CAPTURE_ANALYSIS_PREVIOUS
        command.upgrade(configuration, "head")

        validation_background_job = valid_background_job | {
            "id": uuid4().hex,
            "job_type": "MEDIA_VALIDATION",
            "target_content_type": "image/jpeg",
            "target_size_bytes": 10,
        }
        with engine.begin() as connection:
            connection.execute(
                migrated_metadata.tables["background_job"].insert(),
                validation_background_job,
            )
        with pytest.raises(RuntimeError, match="roll back the application"):
            command.downgrade(configuration, ALEMBIC_MEDIA_VALIDATION_PREVIOUS)
        assert _current_revision(engine) == ALEMBIC_CAPTURE_ANALYSIS_PREVIOUS
        with engine.begin() as connection:
            connection.execute(migrated_metadata.tables["background_job"].delete())
        command.downgrade(configuration, ALEMBIC_MEDIA_VALIDATION_PREVIOUS)
        assert _current_revision(engine) == ALEMBIC_MEDIA_VALIDATION_PREVIOUS
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
