"""Telemetry, structured logging, and correlation tests."""

import json
import logging
import sys
from io import StringIO
from types import TracebackType
from typing import cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from pydantic import AnyHttpUrl

from app.config import AppEnvironment, Settings
from app.contracts.primitives import RequestId, TraceId
from app.platform.observability import (
    CorrelationContext,
    JsonFormatter,
    _create_exporter,
    create_observability,
    current_correlation,
    new_correlation_context,
    set_correlation_job,
    start_server_span,
    use_correlation,
)
from app.runtime import RuntimeKind, create_runtime_context


def _runtime(settings: Settings | None = None):  # type: ignore[no-untyped-def]
    return create_runtime_context(RuntimeKind.API, settings or Settings())


def test_json_formatter_emits_only_safe_correlation_fields() -> None:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        JsonFormatter(
            _runtime(
                Settings(
                    environment=AppEnvironment.STAGING,
                    gcp_project_id="seqret-staging",
                )
            )
        )
    )
    logger = logging.Logger("test")
    logger.addHandler(handler)
    job_id = uuid4()
    correlation = CorrelationContext(
        request_id=RequestId(uuid4()),
        trace_id=TraceId("a" * 32),
        job_id=job_id,
    )
    with use_correlation(correlation):
        logger.info(
            "request finished",
            extra={
                "event": "http_request_complete",
                "http_method": "GET",
                "http_route": "/healthz",
                "http_status": 200,
                "duration_ms": 1.25,
                "outcome": "success",
                "claimed": 3,
                "published": 2,
                "relay_failed": 1,
                "pulled": 4,
                "acknowledged": 3,
                "notification_failed": 1,
            },
        )

    payload = json.loads(stream.getvalue())
    assert payload["request_id"] == str(correlation.request_id)
    assert payload["trace_id"] == "a" * 32
    assert payload["job_id"] == str(job_id)
    assert payload["logging.googleapis.com/trace"].endswith("/" + "a" * 32)
    assert payload["event"] == "http_request_complete"
    assert payload["http_status"] == 200
    assert payload["claimed"] == 3
    assert payload["published"] == 2
    assert payload["relay_failed"] == 1
    assert payload["pulled"] == 4
    assert payload["acknowledged"] == 3
    assert payload["notification_failed"] == 1
    assert "authorization" not in stream.getvalue().lower()
    assert current_correlation() is None


def test_json_formatter_handles_exceptions_and_ignores_unknown_extra() -> None:
    formatter = JsonFormatter(_runtime())
    try:
        raise ValueError("expected")
    except ValueError:
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            "failed",
            (),
            cast(
                tuple[type[BaseException], BaseException, TracebackType | None],
                sys.exc_info(),
            ),
        )
    record.event = 123
    record.http_status = object()

    payload = json.loads(formatter.format(record))

    assert "ValueError: expected" in payload["exception"]
    assert "event" not in payload
    assert "http_status" not in payload


def test_disabled_observability_uses_noop_provider() -> None:
    observability = create_observability(_runtime(Settings(log_level="WARNING")))

    assert observability.tracer_provider is None
    assert observability.logger.level == logging.WARNING
    observability.shutdown()


def test_enabled_observability_builds_and_shuts_down_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = InMemorySpanExporter()
    monkeypatch.setattr("app.platform.observability._create_exporter", lambda _: exporter)
    observability = create_observability(
        _runtime(
            Settings(
                otel_enabled=True,
                otel_exporter_otlp_traces_endpoint=AnyHttpUrl("http://collector:4318/v1/traces"),
                otel_trace_sample_ratio=1,
            )
        )
    )

    assert observability.tracer_provider is not None
    with observability.tracer.start_as_current_span("test"):
        pass
    observability.shutdown()

    assert [span.name for span in exporter.get_finished_spans()] == ["test"]


def test_exporter_uses_plain_session_for_non_google_endpoint() -> None:
    exporter = _create_exporter(
        Settings(
            otel_enabled=True,
            otel_exporter_otlp_traces_endpoint=AnyHttpUrl("http://collector:4318/v1/traces"),
        )
    )

    assert exporter._endpoint == "http://collector:4318/v1/traces"
    cast(object, exporter).shutdown()  # type: ignore[attr-defined]


def test_exporter_uses_adc_for_google_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = Mock()
    session = Mock()
    monkeypatch.setattr(
        "app.platform.observability.google.auth.default", Mock(return_value=(credentials, None))
    )
    authorized_session = Mock(return_value=session)
    monkeypatch.setattr("app.platform.observability.AuthorizedSession", authorized_session)

    exporter = _create_exporter(
        Settings(
            environment=AppEnvironment.STAGING,
            gcp_project_id="seqret-staging",
            otel_enabled=True,
            otel_exporter_otlp_traces_endpoint=AnyHttpUrl(
                "https://telemetry.googleapis.com:443/v1/traces"
            ),
        )
    )

    authorized_session.assert_called_once_with(
        credentials,
        default_host="telemetry.googleapis.com",
    )
    assert exporter._headers == {"x-goog-user-project": "seqret-staging"}
    cast(object, exporter).shutdown()  # type: ignore[attr-defined]


def test_correlation_uses_active_trace_and_adds_job() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=ALWAYS_ON, shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)
    job_id = uuid4()

    with (
        tracer.start_as_current_span("parent") as span,
        use_correlation(new_correlation_context()),
    ):
        correlation = current_correlation()
        assert correlation is not None
        assert correlation.trace_id == trace.format_trace_id(span.get_span_context().trace_id)
        set_correlation_job(job_id)
        assert current_correlation().job_id == job_id  # type: ignore[union-attr]
    provider.shutdown()


def test_set_correlation_job_without_request_is_noop() -> None:
    set_correlation_job(uuid4())
    assert current_correlation() is None


def test_server_span_extracts_w3c_parent() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=ALWAYS_ON, shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)
    remote_trace_id = "1234567890abcdef1234567890abcdef"

    with start_server_span(
        tracer,
        "HTTP GET",
        {"traceparent": f"00-{remote_trace_id}-1234567890abcdef-01"},
        {"http.request.method": "GET"},
    ) as span:
        assert trace.format_trace_id(span.get_span_context().trace_id) == remote_trace_id
    provider.shutdown()


def test_remote_sampling_flag_cannot_override_local_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = InMemorySpanExporter()
    monkeypatch.setattr("app.platform.observability._create_exporter", lambda _: exporter)
    observability = create_observability(
        _runtime(
            Settings(
                otel_enabled=True,
                otel_exporter_otlp_traces_endpoint=AnyHttpUrl("http://collector:4318/v1/traces"),
                otel_trace_sample_ratio=0,
            )
        )
    )

    with start_server_span(
        observability.tracer,
        "HTTP GET",
        {"traceparent": "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01"},
        {"http.request.method": "GET"},
    ) as span:
        assert not span.is_recording()
    observability.shutdown()

    assert exporter.get_finished_spans() == ()


@pytest.mark.parametrize(
    "settings, message",
    [
        (Settings(), "OTLP trace endpoint"),
    ],
)
def test_exporter_requires_endpoint(settings: Settings, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _create_exporter(settings)
