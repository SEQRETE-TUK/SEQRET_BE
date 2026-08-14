"""One-shot Outbox relay and notification event pump Cloud Run Job."""

import asyncio
from contextlib import AsyncExitStack
from typing import cast

from opentelemetry.trace import Status, StatusCode

from app.config import Settings
from app.modules.analysis_workflow.service import dispatch_capture_analyses_once
from app.modules.background_job.service import dispatch_background_jobs_once
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
from app.platform.task_queue import GoogleCloudTasksQueue
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
    task_settings = (
        resolved.gcp_project_id,
        resolved.task_queue_location,
        resolved.task_queue_name,
        resolved.task_worker_url,
        resolved.task_invoker_service_account_email,
    )
    if any(value is None for value in task_settings):
        raise RuntimeError("Cloud Tasks dispatch configuration is required")
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
        task_queue = GoogleCloudTasksQueue(
            cast(str, resolved.gcp_project_id),
            cast(str, resolved.task_queue_location),
            cast(str, resolved.task_worker_url),
            cast(str, resolved.task_invoker_service_account_email),
        )
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
            dispatch_result = await dispatch_background_jobs_once(
                session_factory,
                task_queue,
                queue_name=cast(str, resolved.task_queue_name),
                handler="/tasks/media",
                batch_size=resolved.background_job_batch_size,
                lease_seconds=resolved.background_job_lease_seconds,
                enqueue_timeout_seconds=resolved.task_enqueue_timeout_seconds,
            )
            analysis_dispatch_result = await dispatch_capture_analyses_once(
                session_factory,
                task_queue,
                queue_name=cast(str, resolved.task_queue_name),
                handler="/tasks/analysis",
                batch_size=resolved.background_job_batch_size,
                lease_seconds=resolved.background_job_lease_seconds,
                enqueue_timeout_seconds=resolved.task_enqueue_timeout_seconds,
            )
            failed = (
                relay_result.failed > 0
                or relay_result.claimed != relay_result.published + relay_result.failed
                or consumer_result.failed > 0
                or consumer_result.pulled != consumer_result.acknowledged + consumer_result.failed
                or dispatch_result.failed > 0
                or dispatch_result.claimed != dispatch_result.queued + dispatch_result.failed
                or analysis_dispatch_result.failed > 0
                or analysis_dispatch_result.claimed
                != analysis_dispatch_result.queued + analysis_dispatch_result.failed
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
                    "background_claimed": dispatch_result.claimed,
                    "background_queued": dispatch_result.queued,
                    "background_failed": dispatch_result.failed,
                    "analysis_claimed": analysis_dispatch_result.claimed,
                    "analysis_queued": analysis_dispatch_result.queued,
                    "analysis_failed": analysis_dispatch_result.failed,
                },
            )
            saturated = (
                relay_result.claimed == resolved.outbox_batch_size
                or analysis_dispatch_result.claimed == resolved.background_job_batch_size
            )
            if not failed and saturated:
                observability.logger.warning(
                    "Event pump reached a batch limit",
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
