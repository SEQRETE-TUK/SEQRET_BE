"""Low-overhead ASGI request tracing and structured access logs."""

import logging
from time import perf_counter
from typing import cast

from opentelemetry.trace import Status, StatusCode
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.platform.observability import (
    Observability,
    new_correlation_context,
    start_server_span,
    use_correlation,
)


class HttpObservabilityMiddleware:
    """Trace HTTP requests without recording headers, query strings, or bodies."""

    def __init__(self, app: ASGIApp, observability: Observability) -> None:
        self._app = app
        self._observability = observability

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        method = cast(str, scope["method"])
        request_headers = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in scope["headers"]
            if key in {b"traceparent", b"tracestate"}
        }
        attributes: dict[str, str | int | float] = {
            "http.request.method": method,
            "url.scheme": cast(str, scope["scheme"]),
        }
        status_code = 500
        response_started = False
        error: Exception | None = None
        started_at = perf_counter()

        async def send_with_correlation(message: Message) -> None:
            nonlocal response_started, status_code
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                message["headers"].append(
                    (b"x-request-id", str(correlation.request_id).encode("ascii"))
                )
            await send(message)

        with start_server_span(
            self._observability.tracer,
            f"HTTP {method}",
            request_headers,
            attributes,
        ) as span:
            correlation = new_correlation_context()
            scope.setdefault("state", {})["request_id"] = str(correlation.request_id)
            with use_correlation(correlation):
                try:
                    await self._app(scope, receive, send_with_correlation)
                except Exception as caught:
                    error = caught
                    span.set_attribute("error.type", type(caught).__qualname__)
                    if not response_started:
                        body = b"Internal Server Error"
                        await send_with_correlation(
                            {
                                "type": "http.response.start",
                                "status": 500,
                                "headers": [
                                    (b"content-type", b"text/plain; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode("ascii")),
                                ],
                            }
                        )
                        await send_with_correlation({"type": "http.response.body", "body": body})
                    raise
                finally:
                    route = getattr(scope.get("route"), "path", "unmatched")
                    duration_ms = round((perf_counter() - started_at) * 1000, 3)
                    span.update_name(f"{method} {route}")
                    span.set_attribute("http.route", route)
                    span.set_attribute("http.response.status_code", status_code)
                    failed = error is not None or status_code >= 500
                    if failed:
                        span.set_status(Status(StatusCode.ERROR))
                    log_level = logging.ERROR if failed else logging.INFO
                    self._observability.logger.log(
                        log_level,
                        "HTTP request completed",
                        extra={
                            "event": "http_request_complete",
                            "http_method": method,
                            "http_route": route,
                            "http_status": status_code,
                            "duration_ms": duration_ms,
                            "outcome": "error" if failed else "success",
                        },
                    )
