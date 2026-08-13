"""One-shot Outbox relay entrypoint for a scheduled Cloud Run Job."""

import asyncio
from contextlib import AsyncExitStack

from opentelemetry.trace import Status, StatusCode

from app.config import Settings
from app.platform.db import create_database_engine, create_session_factory
from app.platform.event_bus.google_pubsub import GooglePubSubEventBus
from app.platform.event_bus.service import relay_outbox_once
from app.platform.observability import (
    create_observability,
    new_correlation_context,
    use_correlation,
)
from app.runtime import RuntimeKind, create_runtime_context


async def run(settings: Settings | None = None) -> int:
    """Publish one bounded batch and return failure for retryable Job execution."""

    resolved = settings or Settings()
    if resolved.pubsub_project_id is None or resolved.pubsub_topic_id is None:
        raise RuntimeError("Pub/Sub configuration is required for the Outbox relay")
    observability = create_observability(create_runtime_context(RuntimeKind.JOB, resolved))
    async with AsyncExitStack() as resources:
        resources.callback(observability.shutdown)
        engine = create_database_engine(resolved)
        resources.push_async_callback(engine.dispose)
        event_bus = GooglePubSubEventBus(
            resolved.pubsub_project_id,
            resolved.pubsub_topic_id,
        )
        resources.callback(event_bus.close)
        with (
            observability.tracer.start_as_current_span("outbox.relay_once") as span,
            use_correlation(new_correlation_context()),
        ):
            result = await relay_outbox_once(
                create_session_factory(engine),
                event_bus,
                batch_size=resolved.outbox_batch_size,
                lease_seconds=resolved.outbox_lease_seconds,
                publish_timeout_seconds=resolved.event_publish_timeout_seconds,
            )
            failed = result.failed > 0 or result.claimed != result.published + result.failed
            if failed:
                span.set_status(Status(StatusCode.ERROR))
            log = observability.logger.error if failed else observability.logger.info
            log(
                "Outbox relay completed",
                extra={
                    "event": "outbox_relay_complete",
                    "outcome": "error" if failed else "success",
                },
            )
            if not failed and result.claimed == resolved.outbox_batch_size:
                observability.logger.warning(
                    "Outbox relay reached its batch limit",
                    extra={
                        "event": "outbox_relay_batch_saturated",
                        "outcome": "backlog",
                    },
                )
    return int(failed)


def main() -> None:
    """Run the relay once; the scheduler repeats the Job when needed."""

    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
