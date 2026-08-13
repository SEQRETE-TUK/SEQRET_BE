"""Outbox relay entrypoint lifecycle tests."""

import asyncio
import sys
from collections.abc import Coroutine
from contextlib import nullcontext
from runpy import run_module
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from opentelemetry.trace import StatusCode
from pydantic import SecretStr

from app.config import AppEnvironment, Settings
from app.entrypoints import outbox_relay
from app.modules.notification.consumer import NotificationConsumerResult
from app.platform.event_bus.service import RelayResult


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("relay_result", "consumer_result", "exit_code", "saturated"),
    [
        (
            RelayResult(claimed=1, published=0, failed=1),
            NotificationConsumerResult(pulled=0, acknowledged=0, failed=0),
            1,
            False,
        ),
        (
            RelayResult(claimed=1, published=0, failed=0),
            NotificationConsumerResult(pulled=0, acknowledged=0, failed=0),
            1,
            False,
        ),
        (
            RelayResult(claimed=1, published=1, failed=0),
            NotificationConsumerResult(pulled=1, acknowledged=0, failed=1),
            1,
            False,
        ),
        (
            RelayResult(claimed=1, published=1, failed=0),
            NotificationConsumerResult(pulled=1, acknowledged=0, failed=0),
            1,
            False,
        ),
        (
            RelayResult(claimed=1, published=1, failed=0),
            NotificationConsumerResult(pulled=1, acknowledged=1, failed=0),
            0,
            False,
        ),
        (
            RelayResult(claimed=100, published=100, failed=0),
            NotificationConsumerResult(pulled=0, acknowledged=0, failed=0),
            0,
            True,
        ),
    ],
)
async def test_event_pump_closes_dependencies_and_reports_combined_failure(
    monkeypatch: pytest.MonkeyPatch,
    relay_result: RelayResult,
    consumer_result: NotificationConsumerResult,
    exit_code: int,
    saturated: bool,
) -> None:
    engine = SimpleNamespace(dispose=AsyncMock())
    factory = object()
    bus = SimpleNamespace(close=Mock())
    subscriber = SimpleNamespace(close=Mock())
    relay = AsyncMock(return_value=relay_result)
    consume = AsyncMock(return_value=consumer_result)
    span = Mock()
    observability = SimpleNamespace(
        tracer=Mock(),
        logger=Mock(),
        shutdown=Mock(),
    )
    observability.tracer.start_as_current_span.return_value = nullcontext(span)
    monkeypatch.setattr(outbox_relay, "create_database_engine", lambda _: engine)
    monkeypatch.setattr(outbox_relay, "create_session_factory", lambda _: factory)
    monkeypatch.setattr(outbox_relay, "GooglePubSubEventBus", lambda *_: bus)
    monkeypatch.setattr(outbox_relay, "GooglePubSubPullSubscriber", lambda *_: subscriber)
    monkeypatch.setattr(outbox_relay, "relay_outbox_once", relay)
    monkeypatch.setattr(outbox_relay, "consume_notification_events_once", consume)
    monkeypatch.setattr(
        outbox_relay,
        "create_observability",
        Mock(return_value=observability),
    )
    settings = Settings(
        environment=AppEnvironment.TEST,
        database_url=SecretStr("postgresql+psycopg://seqret:secret@localhost/seqret"),
        pubsub_project_id="seqret-test",
        pubsub_topic_id="domain-events",
        pubsub_subscription_id="participant-notifications",
    )

    assert await outbox_relay.run(settings) == exit_code
    relay.assert_awaited_once_with(
        factory,
        bus,
        batch_size=100,
        lease_seconds=60,
        publish_timeout_seconds=10.0,
    )
    consume.assert_awaited_once_with(
        factory,
        subscriber,
        batch_size=100,
        pull_timeout_seconds=10.0,
    )
    bus.close.assert_called_once_with()
    subscriber.close.assert_called_once_with()
    engine.dispose.assert_awaited_once_with()
    completion_log = getattr(observability.logger, "error" if exit_code else "info")
    completion_log.assert_called_once()
    assert completion_log.call_args.kwargs["extra"] == {
        "event": "outbox_relay_complete",
        "outcome": "error" if exit_code else "success",
        "claimed": relay_result.claimed,
        "published": relay_result.published,
        "relay_failed": relay_result.failed,
        "pulled": consumer_result.pulled,
        "acknowledged": consumer_result.acknowledged,
        "notification_failed": consumer_result.failed,
    }
    if exit_code:
        assert span.set_status.call_args.args[0].status_code is StatusCode.ERROR
    else:
        span.set_status.assert_not_called()
    if saturated:
        observability.logger.warning.assert_called_once()
    else:
        observability.logger.warning.assert_not_called()
    observability.shutdown.assert_called_once_with()


@pytest.mark.anyio
async def test_relay_entrypoint_requires_pubsub_configuration() -> None:
    with pytest.raises(RuntimeError, match="Pub/Sub publication and subscription"):
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
