"""Outbox relay entrypoint lifecycle tests."""

import asyncio
import sys
from collections.abc import Coroutine
from runpy import run_module
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr

from app.config import AppEnvironment, Settings
from app.entrypoints import outbox_relay
from app.platform.event_bus.service import RelayResult


@pytest.mark.anyio
async def test_relay_entrypoint_closes_dependencies_and_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = SimpleNamespace(dispose=AsyncMock())
    factory = object()
    bus = SimpleNamespace(close=Mock())
    relay = AsyncMock(return_value=RelayResult(claimed=1, published=0, failed=1))
    monkeypatch.setattr(outbox_relay, "create_database_engine", lambda _: engine)
    monkeypatch.setattr(outbox_relay, "create_session_factory", lambda _: factory)
    monkeypatch.setattr(outbox_relay, "GooglePubSubEventBus", lambda *_: bus)
    monkeypatch.setattr(outbox_relay, "relay_outbox_once", relay)
    settings = Settings(
        environment=AppEnvironment.TEST,
        database_url=SecretStr("postgresql+psycopg://seqret:secret@localhost/seqret"),
        pubsub_project_id="seqret-test",
        pubsub_topic_id="domain-events",
    )

    assert await outbox_relay.run(settings) == 1
    relay.assert_awaited_once_with(
        factory,
        bus,
        batch_size=100,
        lease_seconds=60,
        publish_timeout_seconds=10.0,
    )
    bus.close.assert_called_once_with()
    engine.dispose.assert_awaited_once_with()


@pytest.mark.anyio
async def test_relay_entrypoint_requires_pubsub_configuration() -> None:
    with pytest.raises(RuntimeError, match="Pub/Sub configuration"):
        await outbox_relay.run(Settings(environment=AppEnvironment.TEST))


def test_relay_main_exits_with_run_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbox_relay, "run", AsyncMock(return_value=0))

    with pytest.raises(SystemExit) as exit_info:
        outbox_relay.main()

    assert exit_info.value.code == 0


def test_relay_module_invokes_main_when_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    def finish_without_running(coroutine: Coroutine[Any, Any, int]) -> int:
        coroutine.close()
        return 0

    monkeypatch.delitem(sys.modules, "app.entrypoints.outbox_relay", raising=False)
    monkeypatch.setattr(asyncio, "run", finish_without_running)

    with pytest.raises(SystemExit) as exit_info:
        run_module("app.entrypoints.outbox_relay", run_name="__main__")

    assert exit_info.value.code == 0
