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
from app.modules.analysis_workflow.service import AnalysisDispatchResult
from app.modules.background_job.service import DispatchResult
from app.modules.notification.consumer import NotificationConsumerResult
from app.modules.notification.delivery import ExternalDeliveryResult
from app.platform.event_bus.service import RelayResult
from app.platform.notification.nhn_cloud import NhnCloudNotificationProvider


@pytest.mark.anyio
@pytest.mark.parametrize(
    (
        "relay_result",
        "consumer_result",
        "dispatch_result",
        "analysis_dispatch_result",
        "exit_code",
        "saturated",
    ),
    [
        (
            RelayResult(claimed=1, published=0, failed=1),
            NotificationConsumerResult(pulled=0, acknowledged=0, failed=0),
            DispatchResult(claimed=0, queued=0, failed=0),
            AnalysisDispatchResult(claimed=0, queued=0, failed=0),
            1,
            False,
        ),
        (
            RelayResult(claimed=1, published=0, failed=0),
            NotificationConsumerResult(pulled=0, acknowledged=0, failed=0),
            DispatchResult(claimed=0, queued=0, failed=0),
            AnalysisDispatchResult(claimed=0, queued=0, failed=0),
            1,
            False,
        ),
        (
            RelayResult(claimed=1, published=1, failed=0),
            NotificationConsumerResult(pulled=1, acknowledged=0, failed=1),
            DispatchResult(claimed=0, queued=0, failed=0),
            AnalysisDispatchResult(claimed=0, queued=0, failed=0),
            1,
            False,
        ),
        (
            RelayResult(claimed=1, published=1, failed=0),
            NotificationConsumerResult(pulled=1, acknowledged=0, failed=0),
            DispatchResult(claimed=0, queued=0, failed=0),
            AnalysisDispatchResult(claimed=0, queued=0, failed=0),
            1,
            False,
        ),
        (
            RelayResult(claimed=1, published=1, failed=0),
            NotificationConsumerResult(pulled=1, acknowledged=1, failed=0),
            DispatchResult(claimed=1, queued=0, failed=1),
            AnalysisDispatchResult(claimed=0, queued=0, failed=0),
            1,
            False,
        ),
        (
            RelayResult(claimed=1, published=1, failed=0),
            NotificationConsumerResult(pulled=1, acknowledged=1, failed=0),
            DispatchResult(claimed=1, queued=1, failed=0),
            AnalysisDispatchResult(claimed=0, queued=0, failed=0),
            0,
            False,
        ),
        (
            RelayResult(claimed=100, published=100, failed=0),
            NotificationConsumerResult(pulled=0, acknowledged=0, failed=0),
            DispatchResult(claimed=0, queued=0, failed=0),
            AnalysisDispatchResult(claimed=0, queued=0, failed=0),
            0,
            True,
        ),
        (
            RelayResult(claimed=0, published=0, failed=0),
            NotificationConsumerResult(pulled=0, acknowledged=0, failed=0),
            DispatchResult(claimed=0, queued=0, failed=0),
            AnalysisDispatchResult(claimed=1, queued=0, failed=1),
            1,
            False,
        ),
        (
            RelayResult(claimed=0, published=0, failed=0),
            NotificationConsumerResult(pulled=0, acknowledged=0, failed=0),
            DispatchResult(claimed=0, queued=0, failed=0),
            AnalysisDispatchResult(claimed=1, queued=0, failed=0),
            1,
            False,
        ),
        (
            RelayResult(claimed=0, published=0, failed=0),
            NotificationConsumerResult(pulled=0, acknowledged=0, failed=0),
            DispatchResult(claimed=0, queued=0, failed=0),
            AnalysisDispatchResult(claimed=100, queued=100, failed=0),
            0,
            True,
        ),
    ],
)
async def test_event_pump_closes_dependencies_and_reports_combined_failure(
    monkeypatch: pytest.MonkeyPatch,
    relay_result: RelayResult,
    consumer_result: NotificationConsumerResult,
    dispatch_result: DispatchResult,
    analysis_dispatch_result: AnalysisDispatchResult,
    exit_code: int,
    saturated: bool,
) -> None:
    engine = SimpleNamespace(dispose=AsyncMock())
    factory = object()
    bus = SimpleNamespace(close=Mock())
    subscriber = SimpleNamespace(close=Mock())
    task_queue = object()
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
    monkeypatch.setattr(outbox_relay, "GoogleCloudTasksQueue", lambda *_: task_queue)
    monkeypatch.setattr(
        outbox_relay,
        "relay_outbox_once",
        AsyncMock(return_value=relay_result),
    )
    monkeypatch.setattr(
        outbox_relay,
        "consume_notification_events_once",
        AsyncMock(return_value=consumer_result),
    )
    monkeypatch.setattr(
        outbox_relay,
        "dispatch_background_jobs_once",
        AsyncMock(return_value=dispatch_result),
    )
    monkeypatch.setattr(
        outbox_relay,
        "dispatch_capture_analyses_once",
        AsyncMock(return_value=analysis_dispatch_result),
    )
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
        gcp_project_id="seqret-test",
        task_queue_location="asia-northeast3",
        task_queue_name="seqret-test-media",
        task_worker_url="https://seqret-test-worker.run.app",
        task_invoker_service_account_email=("seqret-stg-tasks@seqret-test.iam.gserviceaccount.com"),
    )

    assert await outbox_relay.run(settings) == exit_code
    bus.close.assert_called_once_with()
    subscriber.close.assert_called_once_with()
    engine.dispose.assert_awaited_once_with()
    completion_log = getattr(observability.logger, "error" if exit_code else "info")
    completion_log.assert_called_once()
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

    with pytest.raises(RuntimeError, match="Cloud Tasks dispatch"):
        await outbox_relay.run(
            Settings(
                environment=AppEnvironment.TEST,
                pubsub_project_id="seqret-test",
                pubsub_topic_id="domain-events",
                pubsub_subscription_id="participant-notifications",
            )
        )


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


def _notification_settings() -> Settings:
    return Settings(
        environment=AppEnvironment.TEST,
        database_url=SecretStr("postgresql+psycopg://seqret:secret@localhost/seqret"),
        pubsub_project_id="seqret-test",
        pubsub_topic_id="domain-events",
        pubsub_subscription_id="participant-notifications",
        gcp_project_id="seqret-test",
        task_queue_location="asia-northeast3",
        task_queue_name="seqret-test-media",
        task_worker_url="https://seqret-test-worker.run.app",
        task_invoker_service_account_email=("seqret-stg-tasks@seqret-test.iam.gserviceaccount.com"),
        frontend_origin="https://seqret.example.com",
        notification_delivery_enabled=True,
        nhn_notification_email_app_key="email-app",
        nhn_notification_email_secret_key=SecretStr("email-secret"),
        nhn_notification_email_sender_address="notice@seqret.example.com",
        nhn_notification_email_sender_name="SEQRET",
        nhn_notification_sms_app_key="sms-app",
        nhn_notification_sms_secret_key=SecretStr("sms-secret"),
        nhn_notification_sms_sender_number="0212345678",
        nhn_notification_kakao_app_key="kakao-app",
        nhn_notification_kakao_secret_key=SecretStr("kakao-secret"),
        nhn_notification_kakao_sender_key="a" * 40,
        nhn_notification_kakao_template_code="SEQRET_NOTICE",
    )


def test_notification_provider_factory_is_disabled_or_complete() -> None:
    assert outbox_relay._create_notification_provider(Settings()) is None
    assert isinstance(
        outbox_relay._create_notification_provider(_notification_settings()),
        NhnCloudNotificationProvider,
    )
    unsafe = Settings.model_construct(notification_delivery_enabled=True)
    with pytest.raises(RuntimeError, match="configuration is incomplete"):
        outbox_relay._create_notification_provider(unsafe)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("delivery_result", "exit_code", "warns"),
    [
        (ExternalDeliveryResult(1, 0, 0, 1), 1, False),
        (ExternalDeliveryResult(100, 100, 0, 0), 0, True),
        (ExternalDeliveryResult(1, 0, 0, 0), 1, False),
    ],
)
async def test_event_pump_runs_enabled_external_delivery(
    monkeypatch: pytest.MonkeyPatch,
    delivery_result: ExternalDeliveryResult,
    exit_code: int,
    warns: bool,
) -> None:
    engine = SimpleNamespace(dispose=AsyncMock())
    factory = object()
    bus = SimpleNamespace(close=Mock())
    subscriber = SimpleNamespace(close=Mock())
    observability = SimpleNamespace(
        tracer=Mock(),
        logger=Mock(),
        shutdown=Mock(),
    )
    observability.tracer.start_as_current_span.return_value = nullcontext(Mock())
    monkeypatch.setattr(outbox_relay, "create_database_engine", lambda _: engine)
    monkeypatch.setattr(outbox_relay, "create_session_factory", lambda _: factory)
    monkeypatch.setattr(outbox_relay, "GooglePubSubEventBus", lambda *_: bus)
    monkeypatch.setattr(outbox_relay, "GooglePubSubPullSubscriber", lambda *_: subscriber)
    monkeypatch.setattr(outbox_relay, "GoogleCloudTasksQueue", lambda *_: object())
    monkeypatch.setattr(
        outbox_relay,
        "relay_outbox_once",
        AsyncMock(return_value=RelayResult(0, 0, 0)),
    )
    monkeypatch.setattr(
        outbox_relay,
        "consume_notification_events_once",
        AsyncMock(return_value=NotificationConsumerResult(0, 0, 0)),
    )
    monkeypatch.setattr(
        outbox_relay,
        "deliver_external_notifications_once",
        AsyncMock(return_value=delivery_result),
    )
    monkeypatch.setattr(
        outbox_relay,
        "dispatch_background_jobs_once",
        AsyncMock(return_value=DispatchResult(0, 0, 0)),
    )
    monkeypatch.setattr(
        outbox_relay,
        "dispatch_capture_analyses_once",
        AsyncMock(return_value=AnalysisDispatchResult(0, 0, 0)),
    )
    monkeypatch.setattr(outbox_relay, "create_observability", Mock(return_value=observability))

    assert await outbox_relay.run(_notification_settings()) == exit_code
    if warns:
        observability.logger.warning.assert_called_once()
    else:
        observability.logger.warning.assert_not_called()
