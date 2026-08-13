"""Vendor-neutral telemetry and Cloud Logging compatible JSON output."""

import json
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import google.auth
from google.auth.transport.requests import AuthorizedSession
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
from opentelemetry.trace import Span, SpanKind, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from app import __version__
from app.config import Settings
from app.contracts.primitives import RequestId, TraceId
from app.runtime import RuntimeContext

_correlation: ContextVar["CorrelationContext | None"] = ContextVar(
    "seqret_correlation",
    default=None,
)


@dataclass(frozen=True, slots=True)
class CorrelationContext:
    """Identifiers safe to attach to logs and spans."""

    request_id: RequestId
    trace_id: TraceId
    job_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class Observability:
    """Per-runtime telemetry resources with deterministic shutdown."""

    tracer: Tracer
    tracer_provider: TracerProvider | None
    logger: logging.Logger

    def shutdown(self) -> None:
        if self.tracer_provider is not None:
            self.tracer_provider.shutdown()


class JsonFormatter(logging.Formatter):
    """Emit one compact structured event without serializing arbitrary objects."""

    def __init__(self, runtime: RuntimeContext) -> None:
        super().__init__()
        self._runtime = runtime

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "service": self._runtime.service_name,
            "environment": self._runtime.settings.environment.value,
            "runtime": self._runtime.kind.value,
        }
        correlation = current_correlation()
        if correlation is not None:
            payload["request_id"] = str(correlation.request_id)
            payload["trace_id"] = correlation.trace_id
            if correlation.job_id is not None:
                payload["job_id"] = str(correlation.job_id)
            project_id = self._runtime.settings.gcp_project_id
            if project_id is not None:
                payload["logging.googleapis.com/trace"] = (
                    f"projects/{project_id}/traces/{correlation.trace_id}"
                )
        event = getattr(record, "event", None)
        if isinstance(event, str):
            payload["event"] = event
        for key in (
            "http_method",
            "http_route",
            "http_status",
            "duration_ms",
            "outcome",
            "claimed",
            "published",
            "relay_failed",
            "pulled",
            "acknowledged",
            "notification_failed",
            "background_claimed",
            "background_queued",
            "background_failed",
        ):
            value = getattr(record, key, None)
            if isinstance(value, str | int | float):
                payload[key] = value
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logger(runtime: RuntimeContext) -> logging.Logger:
    """Create an isolated runtime logger without replacing application-wide handlers."""

    logger = logging.Logger(f"app.{runtime.kind.value}")
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(runtime))
    logger.addHandler(handler)
    logger.setLevel(runtime.settings.log_level)
    logger.propagate = False
    return logger


def _create_exporter(settings: Settings) -> OTLPSpanExporter:
    endpoint = settings.otel_exporter_otlp_traces_endpoint
    if endpoint is None:
        raise ValueError("OTLP trace endpoint is required")
    endpoint_value = str(endpoint)
    if endpoint_value.startswith("https://telemetry.googleapis.com/"):
        credentials, _ = google.auth.default(
            scopes=("https://www.googleapis.com/auth/trace.append",),
            quota_project_id=settings.gcp_project_id,
        )
        session = AuthorizedSession(  # type: ignore[no-untyped-call]
            credentials,
            default_host="telemetry.googleapis.com",
        )
        headers = (
            {"x-goog-user-project": settings.gcp_project_id}
            if settings.gcp_project_id is not None
            else None
        )
        return OTLPSpanExporter(endpoint=endpoint_value, headers=headers, session=session)
    return OTLPSpanExporter(endpoint=endpoint_value)


def create_observability(runtime: RuntimeContext) -> Observability:
    """Configure sampled OTLP traces and structured logs for one runtime."""

    logger = configure_logger(runtime)
    if not runtime.settings.otel_enabled:
        return Observability(
            tracer=trace.NoOpTracerProvider().get_tracer(__name__, __version__),
            tracer_provider=None,
            logger=logger,
        )
    resource = Resource.create(
        {
            "service.name": runtime.service_name,
            "service.version": __version__,
            "deployment.environment.name": runtime.settings.environment.value,
        }
    )
    provider = TracerProvider(
        sampler=TraceIdRatioBased(runtime.settings.otel_trace_sample_ratio),
        resource=resource,
        shutdown_on_exit=False,
    )
    provider.add_span_processor(BatchSpanProcessor(_create_exporter(runtime.settings)))
    return Observability(
        tracer=provider.get_tracer(__name__, __version__),
        tracer_provider=provider,
        logger=logger,
    )


def _trace_id_from_span() -> TraceId:
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        return TraceId(trace.format_trace_id(span_context.trace_id))
    return TraceId(uuid4().hex)


def new_correlation_context() -> CorrelationContext:
    """Create request correlation from the current remote or local span."""

    return CorrelationContext(request_id=RequestId(uuid4()), trace_id=_trace_id_from_span())


def current_correlation() -> CorrelationContext | None:
    return _correlation.get()


def set_correlation_job(job_id: UUID) -> None:
    current = current_correlation()
    if current is not None:
        _correlation.set(
            CorrelationContext(
                request_id=current.request_id,
                trace_id=current.trace_id,
                job_id=job_id,
            )
        )


@contextmanager
def use_correlation(correlation: CorrelationContext) -> Iterator[None]:
    token: Token[CorrelationContext | None] = _correlation.set(correlation)
    try:
        yield
    finally:
        _correlation.reset(token)


@contextmanager
def start_server_span(
    tracer: Tracer,
    name: str,
    headers: Mapping[str, str],
    attributes: Mapping[str, str | int | float],
) -> Iterator[Span]:
    """Extract W3C context and create one HTTP server span."""

    extracted = TraceContextTextMapPropagator().extract(headers)
    with tracer.start_as_current_span(
        name,
        context=extracted,
        kind=SpanKind.SERVER,
        attributes=dict(attributes),
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        yield span
