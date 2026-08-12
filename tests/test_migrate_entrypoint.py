"""Deployment migration gate tests."""

import sys
from contextlib import nullcontext
from runpy import run_module
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from app.config import AppEnvironment, Settings
from app.entrypoints import migrate


def test_migration_gate_upgrades_single_head_and_shuts_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observability = SimpleNamespace(
        tracer=Mock(),
        logger=Mock(),
        shutdown=Mock(),
    )
    observability.tracer.start_as_current_span.return_value = nullcontext()
    monkeypatch.setattr(migrate, "create_observability", Mock(return_value=observability))
    upgrade = Mock()
    monkeypatch.setattr("app.entrypoints.migrate.command.upgrade", upgrade)
    settings = Settings(
        environment=AppEnvironment.TEST,
        database_url=SecretStr("postgresql+psycopg://seqret:p%40ss@localhost/seqret"),
    )

    migrate.run(settings)

    config, revision = upgrade.call_args.args
    assert config.get_main_option("sqlalchemy.url").endswith("p%40ss@localhost/seqret")
    assert revision == "head"
    observability.shutdown.assert_called_once_with()


def test_migration_gate_requires_database_configuration() -> None:
    with pytest.raises(RuntimeError, match="Database configuration"):
        migrate.run(Settings(environment=AppEnvironment.TEST))


def test_migration_main_uses_environment_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock()
    monkeypatch.setattr(migrate, "run", run)

    migrate.main()

    run.assert_called_once_with()


def test_migration_module_invokes_main(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "app.entrypoints.migrate", raising=False)
    monkeypatch.setattr("alembic.command.upgrade", Mock())
    monkeypatch.setenv(
        "SEQRET_DATABASE_URL",
        "postgresql+psycopg://seqret:secret@localhost/seqret",
    )

    run_module("app.entrypoints.migrate", run_name="__main__")
