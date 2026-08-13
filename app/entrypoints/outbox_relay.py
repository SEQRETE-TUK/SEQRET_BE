"""One-shot Outbox relay and notification event pump Cloud Run Job."""

import asyncio
from contextlib import AsyncExitStack

from opentelemetry.trace import Status, StatusCode

from app.config import Settings
from app.modules.notification.consumer import consume_notification_events_once
from app.platform.db import create_database_engine, create_session_factory
from app.platform.event_bus.google_pubsub import GooglePubSubEventBus
from app.platform.event_bus.google_pubsub_subscriber import GooglePubSubPullSubscriber
from app.platform.event_bus.service import relay_outbox_once
from app.platform.observability import (
    create_observability,
    new_correlation_context,
    use_correlation,
)
from app.runtime import RuntimeKind, create_runtime_context


async def run(settings: Settings | None = None) -> int:
    """Publish then consume bounded batches, returning one combined Job result."""

    resolved = settings or Settings()
    if (
        resolved.pubsub_project_id is None
        or resolved.pubsub_topic_id is None
        or resolved.pubsub_subscription_id is None
    ):
        raise RuntimeError("Pub/Sub publication and subscription configuration is required")
    observability = create_observability(create_runtime_context(RuntimeKind.JOB, resolved))
    async with AsyncExitStack() as resources:
        resources.callback(observability.shutdown)
        engine = create_database_engine(resolved)
        resources.push_async_callback(engine.dispose)
        session_factory = create_session_factory(engine)
        event_bus = GooglePubSubEventBus(
            resolved.pubsub_project_id,
            resolved.pubsub_topic_id,
        )
        resources.callback(event_bus.close)
        subscriber = GooglePubSubPullSubscriber(
            resolved.pubsub_project_id,
            resolved.pubsub_subscription_id,
            resolved.pubsub_topic_id,
        )
        resources.callback(subscriber.close)
        with (
            observability.tracer.start_as_current_span("outbox.event_pump_once") as span,
            use_correlation(new_correlation_context()),
        ):
            relay_result = await relay_outbox_once(
                session_factory,
                event_bus,
                batch_size=resolved.outbox_batch_size,
                lease_seconds=resolved.outbox_lease_seconds,
                publish_timeout_seconds=resolved.event_publish_timeout_seconds,
            )
            consumer_result = await consume_notification_events_once(
                session_factory,
                subscriber,
                batch_size=resolved.notification_batch_size,
                pull_timeout_seconds=resolved.notification_pull_timeout_seconds,
            )
            failed = (
                relay_result.failed > 0
                or relay_result.claimed != relay_result.published + relay_result.failed
                or consumer_result.failed > 0
                or consumer_result.pulled != consumer_result.acknowledged + consumer_result.failed
            )
            if failed:
                span.set_status(Status(StatusCode.ERROR))
            log = observability.logger.error if failed else observability.logger.info
            log(
                "Outbox event pump completed",
                extra={
                    "event": "outbox_relay_complete",
                    "outcome": "error" if failed else "success",
                    "claimed": relay_result.claimed,
                    "published": relay_result.published,
                    "relay_failed": relay_result.failed,
                    "pulled": consumer_result.pulled,
                    "acknowledged": consumer_result.acknowledged,
                    "notification_failed": consumer_result.failed,
                },
            )
            if not failed and relay_result.claimed == resolved.outbox_batch_size:
                observability.logger.warning(
                    "Outbox relay reached its batch limit",
                    extra={
                        "event": "outbox_relay_batch_saturated",
                        "outcome": "backlog",
                    },
                )
    return int(failed)


def main() -> None:
    """Run the event pump once; the scheduler repeats the Job when needed."""

    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
