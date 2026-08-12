"""One-shot Outbox relay entrypoint for a scheduled Cloud Run Job."""

import asyncio

from app.config import Settings
from app.platform.db import create_database_engine, create_session_factory
from app.platform.event_bus.google_pubsub import GooglePubSubEventBus
from app.platform.event_bus.service import relay_outbox_once


async def run(settings: Settings | None = None) -> int:
    """Publish one bounded batch and return failure for retryable Job execution."""

    resolved = settings or Settings()
    if resolved.pubsub_project_id is None or resolved.pubsub_topic_id is None:
        raise RuntimeError("Pub/Sub configuration is required for the Outbox relay")
    engine = create_database_engine(resolved)
    event_bus = GooglePubSubEventBus(
        resolved.pubsub_project_id,
        resolved.pubsub_topic_id,
    )
    try:
        result = await relay_outbox_once(
            create_session_factory(engine),
            event_bus,
            batch_size=resolved.outbox_batch_size,
            lease_seconds=resolved.outbox_lease_seconds,
            publish_timeout_seconds=resolved.event_publish_timeout_seconds,
        )
    finally:
        event_bus.close()
        await engine.dispose()
    return int(result.failed > 0)


def main() -> None:
    """Run the relay once; the scheduler repeats the Job when needed."""

    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
