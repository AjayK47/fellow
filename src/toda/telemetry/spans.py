"""Small OpenTelemetry helpers used by Toda application code."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from contextlib import AbstractContextManager
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry.trace import Link, Span, SpanKind, Status, StatusCode

from toda.services.redaction import redact
from toda.telemetry.attributes import (
    TODA_INPUT_CHARS,
    TODA_OPERATION_OUTCOME,
    TODA_OUTPUT_CHARS,
    TODA_PAYLOAD_CAPTURED,
    TODA_PAYLOAD_HASH,
    correlation_attributes,
)

log = logging.getLogger("toda.telemetry")

_INHERITED_ATTRIBUTES = (
    "toda.organization.id", "toda.session.id", "toda.task.id", "toda.root_task.id",
    "toda.job.id", "toda.job.kind", "toda.agent.type", "toda.agent.iteration",
    "toda.worker.id", "toda.approval.id", "toda.approval.state", "toda.operation.risk",
    "toda.prompt.version", "toda.prompt.hash", "toda.toolset.hash", "toda.skillset.hash",
    "toda.policy.hash", "toda.catalog.version", "toda.catalog.fetched_at",
)
_CORRELATION_CONTEXT_KEYS = {
    key: otel_context.create_key(f"toda.local_correlation.{key}")
    for key in _INHERITED_ATTRIBUTES
}

_SECRET_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "cookie",
    "password",
    "secret",
    "token",
}


def _bounded(value: Any, *, max_bytes: int, key: str = "") -> Any:
    if key.lower() in _SECRET_KEYS or any(marker in key.lower() for marker in _SECRET_KEYS):
        return "[REDACTED]"
    if isinstance(value, dict):
        result = {str(k): _bounded(v, max_bytes=max_bytes, key=str(k)) for k, v in value.items()}
    elif isinstance(value, (list, tuple)):
        result = [_bounded(v, max_bytes=max_bytes) for v in value]
    elif isinstance(value, str):
        result = redact(value).text
    elif isinstance(value, (int, float, bool)) or value is None:
        result = value
    else:
        result = str(value)
    try:
        encoded = json.dumps(result, default=str)
    except Exception:
        encoded = str(result)
    if len(encoded.encode("utf-8")) <= max_bytes:
        return result
    return {"truncated": True, "chars": len(encoded), "sha256": hashlib.sha256(encoded.encode()).hexdigest()}


def _payload_metadata(value: Any) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, default=str, sort_keys=True)
    except Exception:
        encoded = str(value)
    return {
        "chars": len(encoded),
        "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


class TelemetrySpan(AbstractContextManager["TelemetrySpan"]):
    """A thin lifecycle helper around a real OpenTelemetry Span."""

    def __init__(
        self,
        telemetry: "Telemetry",
        name: str,
        *,
        attributes: dict[str, Any] | None = None,
        input: Any = None,
        context: dict[str, Any] | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        links: list[Link] | None = None,
    ) -> None:
        self.telemetry = telemetry
        self.name = name
        self.input = input
        self.context = context or {}
        self.attributes = {**correlation_attributes(self.context), **(attributes or {})}
        self.kind = kind
        self.links = list(links or [])
        self.span: Span | None = None
        self._cm: Any = None
        self._output: Any = None
        self._error: Any = None
        self._started_at = 0.0
        self._correlation_token: Any = None
        self._business_outcome = self.attributes.get(TODA_OPERATION_OUTCOME)

    def __enter__(self) -> "TelemetrySpan":
        self._started_at = time.perf_counter()
        inherited = {
            key: otel_context.get_value(_CORRELATION_CONTEXT_KEYS[key])
            for key in _INHERITED_ATTRIBUTES
            if otel_context.get_value(_CORRELATION_CONTEXT_KEYS[key]) is not None
        }
        span_attributes = {**inherited, **self.attributes}
        self._cm = self.telemetry.tracer.start_as_current_span(
            self.name,
            kind=self.kind,
            links=self.links,
            attributes=span_attributes,
            record_exception=False,
            set_status_on_exception=False,
        )
        self.span = self._cm.__enter__()
        correlation_context = otel_context.get_current()
        for key, value in span_attributes.items():
            if key.startswith("toda.") and isinstance(value, (str, int, float, bool)):
                context_key = _CORRELATION_CONTEXT_KEYS.get(key)
                if context_key is not None:
                    correlation_context = otel_context.set_value(
                        context_key, value, correlation_context
                    )
        self._correlation_token = otel_context.attach(correlation_context)
        if self.input is not None:
            self._add_payload("input", self.input)
        return self

    def _add_payload(self, prefix: str, value: Any) -> None:
        if self.span is None:
            return
        metadata = _payload_metadata(value)
        self.span.set_attribute(
            TODA_INPUT_CHARS if prefix == "input" else TODA_OUTPUT_CHARS,
            metadata["chars"],
        )
        self.span.set_attribute(
            f"toda.{prefix}.sha256", metadata["sha256"]
        )
        if self.telemetry.capture_payloads:
            self.span.set_attribute(TODA_PAYLOAD_CAPTURED, True)
            self.span.set_attribute(
                f"toda.debug.{prefix}",
                json.dumps(_bounded(value, max_bytes=self.telemetry.max_payload_bytes), default=str),
            )
        else:
            self.span.set_attribute(TODA_PAYLOAD_CAPTURED, False)
            self.span.set_attribute(TODA_PAYLOAD_HASH, metadata["sha256"])

    def output(self, value: Any) -> None:
        self._output = value
        self._add_payload("output", value)

    def error(self, value: Any) -> None:
        self._error = value

    def set_attribute(self, key: str, value: Any) -> None:
        if self.span is not None and isinstance(value, (str, int, float, bool)):
            self.span.set_attribute(key, value)

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        if self.span is not None:
            safe = {
                key: value
                for key, value in (attributes or {}).items()
                if isinstance(value, (str, int, float, bool))
            }
            self.span.add_event(name, attributes=safe)

    def _failed(self, error: Any) -> None:
        if self.span is None:
            return
        if isinstance(error, BaseException):
            error_type = type(error).__name__
            error_message = str(error)
        elif isinstance(error, dict):
            error_type = str(error.get("type") or "Error")
            error_message = str(error.get("message") or error)
        else:
            error_type = type(error).__name__
            error_message = str(error)
        safe_message = redact(error_message).text[: self.telemetry.max_payload_bytes]
        self.span.set_status(Status(StatusCode.ERROR, error_type))
        self.span.set_attribute("error.type", error_type)
        self.span.add_event(
            "exception",
            attributes={"exception.type": error_type, "exception.message": safe_message},
        )
        self.span.set_attribute(TODA_OPERATION_OUTCOME, "failed")

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        outcome = (
            "failed"
            if exc is not None or self._error is not None
            else str(self._business_outcome or "completed")
        )
        try:
            if exc is not None:
                self._failed(exc)
            elif self._error is not None:
                self._failed(self._error)
            elif self.span is not None:
                # Business outcomes such as denied, waiting, and skipped are
                # successful executions from OTel's perspective. Preserve
                # the explicit outcome instead of replacing it with the
                # generic lifecycle value on exit.
                self.span.set_attribute(
                    TODA_OPERATION_OUTCOME, self._business_outcome or "completed"
                )
        finally:
            if self._correlation_token is not None:
                otel_context.detach(self._correlation_token)
            if self._cm is not None:
                self._cm.__exit__(exc_type, exc, tb)
            if self.telemetry.metrics is not None:
                try:
                    self.telemetry.metrics.counter(
                        "toda_spans_total", labels={"operation": self.name, "outcome": outcome}
                    )
                    self.telemetry.metrics.histogram(
                        "toda_span_duration_ms",
                        (time.perf_counter() - self._started_at) * 1000,
                        labels={"operation": self.name},
                    )
                    metric_stems = {
                        "attention.classify": "attention_requests",
                        "context.build": "context_build",
                        "retrieval.search": "retrieval_requests",
                        "model.embed": "embedding_requests",
                        "connector.request": "connector_requests",
                        "memory.capture": "memory_capture",
                        "delivery.send": "delivery_requests",
                        "media.generate": "media_requests",
                    }
                    stem = metric_stems.get(self.name)
                    if stem:
                        self.telemetry.metrics.counter(
                            f"toda_{stem}_total",
                            labels={"operation": self.name, "outcome": outcome},
                        )
                        self.telemetry.metrics.histogram(
                            f"toda_{stem}_duration_ms",
                            (time.perf_counter() - self._started_at) * 1000,
                            labels={"operation": self.name},
                        )
                except Exception:
                    log.debug("telemetry.metric_record_failed", exc_info=True)
        return False


class Telemetry:
    """Store-owned facade over an OpenTelemetry TracerProvider."""

    def __init__(
        self,
        *,
        provider: Any,
        local_exporter: Any,
        metrics: Any | None = None,
        enabled: bool = True,
        capture_payloads: bool = False,
        max_payload_bytes: int = 100_000,
        service_name: str = "toda",
    ) -> None:
        self.provider = provider
        self.local_exporter = local_exporter
        self.metrics = metrics
        self.tracer = provider.get_tracer("toda", service_name)
        self.enabled = enabled
        self.capture_payloads = capture_payloads
        self.max_payload_bytes = max_payload_bytes
        self.service_name = service_name

    def span(
        self,
        name: str,
        *,
        component: str | None = None,
        input: Any = None,
        attributes: dict[str, Any] | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        links: list[Link] | None = None,
        **context: Any,
    ) -> TelemetrySpan:
        attrs = dict(attributes or {})
        if component:
            attrs.setdefault("toda.component", component)
        return TelemetrySpan(
            self,
            name,
            attributes=attrs,
            input=input,
            context=context,
            kind=kind,
            links=links,
        )

    def record(
        self,
        operation: str,
        *,
        component: str | None = None,
        phase: str = "event",
        input: Any = None,
        output: Any = None,
        error: Any = None,
        **context: Any,
    ) -> dict[str, Any] | None:
        """Record one completed OTel span for an instantaneous operation."""

        with self.span(
            operation,
            component=component,
            input=input if phase == "started" else None,
            attributes={TODA_OPERATION_OUTCOME: phase},
            **context,
        ) as span:
            if output is not None:
                span.output(output)
            if error is not None or phase in {"failed", "error"}:
                span.error(error or {"type": "OperationError", "message": phase})
        return None

    def force_flush(self) -> bool:
        try:
            return bool(self.provider.force_flush())
        except Exception:
            log.exception("telemetry.force_flush_failed")
            return False

    def metrics_snapshot(self) -> dict[str, Any]:
        return self.metrics.snapshot() if self.metrics is not None else {"counters": []}

    def shutdown(self) -> None:
        try:
            self.provider.shutdown()
        except Exception:
            log.exception("telemetry.shutdown_failed")
        if self.metrics is not None:
            try:
                self.metrics.shutdown()
            except Exception:
                log.exception("telemetry.metrics_shutdown_failed")
