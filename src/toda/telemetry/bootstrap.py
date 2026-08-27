"""OpenTelemetry provider and instrumentation bootstrap."""

from __future__ import annotations

import logging
import os
import socket
import threading
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from opentelemetry import propagate
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from toda.telemetry.sqlite_exporter import SQLiteSpanExporter
from toda.telemetry.metrics import TelemetryMetrics
from toda.telemetry.spans import Telemetry

log = logging.getLogger("toda.telemetry.bootstrap")
_httpx_lock = threading.Lock()
_httpx_instrumented = False


def _config_value(config: Any, name: str, default: Any) -> Any:
    return getattr(config, name, default) if config is not None else default


def build_telemetry(*, store: Any, config: Any | None = None) -> Telemetry:
    enabled = bool(_config_value(config, "enabled", True))
    service_name = str(_config_value(config, "service_name", "toda"))
    environment = str(_config_value(config, "environment", "development"))
    sample_ratio = float(_config_value(config, "sample_ratio", 1.0))
    sample_ratio = min(1.0, max(0.0, sample_ratio))
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": "0.1.0",
            "service.instance.id": f"{socket.gethostname()}:{os.getpid()}",
            "deployment.environment.name": environment,
        }
    )
    # Toda correlation is intentionally local. A trace context is the only
    # propagation data allowed to leave the process; in particular, incoming
    # baggage must not reach provider, Slack, MCP, or Composio endpoints.
    propagate.set_global_textmap(TraceContextTextMapPropagator())
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(sample_ratio if enabled else 0.0)),
    )
    local_exporter = SQLiteSpanExporter(store)
    endpoint = _config_value(config, "otlp_endpoint", None)
    headers = dict(_config_value(config, "otlp_headers", {}) or {})
    timeout = float(_config_value(config, "export_timeout_seconds", 10.0))
    metrics = TelemetryMetrics(
        otlp_endpoint=str(endpoint) if enabled and endpoint else None,
        otlp_headers=headers,
        export_timeout_seconds=timeout,
    )
    # SQLite must be immediately queryable by the local console and tests.
    provider.add_span_processor(SimpleSpanProcessor(local_exporter))
    if enabled and bool(_config_value(config, "console_export", False)):
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    if enabled and endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            exporter = OTLPSpanExporter(
                endpoint=str(endpoint), headers=headers, timeout=timeout
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))
        except Exception:
            log.exception("telemetry.otlp_setup_failed")

    return Telemetry(
        provider=provider,
        local_exporter=local_exporter,
        metrics=metrics,
        enabled=enabled,
        capture_payloads=bool(_config_value(config, "capture_payloads", False)),
        max_payload_bytes=int(_config_value(config, "max_payload_bytes", 100_000)),
        service_name=service_name,
    )


def instrument_fastapi(app: Any, telemetry: Telemetry) -> None:
    if getattr(app.state, "toda_fastapi_instrumented", False):
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, tracer_provider=telemetry.provider)
        app.state.toda_fastapi_instrumented = True
    except Exception:
        log.exception("telemetry.fastapi_instrumentation_failed")


def instrument_httpx_once(telemetry: Telemetry) -> None:
    global _httpx_instrumented
    with _httpx_lock:
        if _httpx_instrumented:
            return
        try:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

            def scrub_request(span: Any, request: Any) -> None:
                """Keep auto-generated HTTP spans safe even if header capture is enabled."""
                if not span.is_recording():
                    return
                try:
                    raw_url_value = getattr(request, "url", "")
                    raw_url = (
                        raw_url_value.decode("utf-8", "replace")
                        if isinstance(raw_url_value, bytes)
                        else str(raw_url_value)
                    )
                    parsed = urlsplit(raw_url)
                    safe_url = urlunsplit(
                        (parsed.scheme, parsed.netloc, parsed.path, "", "")
                    )
                    span.set_attribute("http.url", safe_url)
                    span.set_attribute("url.full", safe_url)
                    if parsed.query:
                        span.set_attribute("toda.http.query_present", True)
                    for header in (
                        "authorization",
                        "proxy-authorization",
                        "cookie",
                        "set-cookie",
                        "x-api-key",
                        "api-key",
                    ):
                        if getattr(request, "headers", {}).get(header):
                            span.set_attribute(
                                f"http.request.header.{header}", "[REDACTED]"
                            )
                except Exception:
                    log.debug("telemetry.httpx_request_scrub_failed", exc_info=True)

            def scrub_response(span: Any, _request: Any, response: Any) -> None:
                if not span.is_recording():
                    return
                try:
                    headers = getattr(response, "headers", {})
                    for header in ("authorization", "cookie", "set-cookie", "x-api-key", "api-key"):
                        if headers.get(header):
                            span.set_attribute(
                                f"http.response.header.{header}", "[REDACTED]"
                            )
                except Exception:
                    log.debug("telemetry.httpx_response_scrub_failed", exc_info=True)

            HTTPXClientInstrumentor().instrument(
                tracer_provider=telemetry.provider,
                request_hook=scrub_request,
                response_hook=scrub_response,
            )
            _httpx_instrumented = True
        except Exception:
            log.exception("telemetry.httpx_instrumentation_failed")
