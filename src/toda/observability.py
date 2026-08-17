"""Structured, correlated execution tracing for the Toda harness.

The recorder is intentionally dependency-light.  It writes immutable trace
events to the same durable store as the rest of Toda and mirrors them to the
standard logger.  Payloads are bounded and recursively redacted before either
sink sees them.
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from toda.domain.models import new_id

log = logging.getLogger("toda.trace")

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("toda_trace_id", default=None)
_span_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("toda_span_id", default=None)
_correlation: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("toda_trace_context", default={})
_SECRET_MARKERS = ("token", "secret", "password", "authorization", "api_key", "apikey", "cookie")


class JsonLogFormatter(logging.Formatter):
    """Small stdlib-compatible JSON formatter for operator logs."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **current_context(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(_safe(payload), default=str)


def configure_json_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def current_context() -> dict[str, Any]:
    return dict(_correlation.get() or {})


def bind_context(**values: Any) -> tuple[contextvars.Token, contextvars.Token, contextvars.Token]:
    values = {key: value for key, value in values.items() if value not in (None, "")}
    trace = str(values.pop("trace_id", None) or _trace_id.get() or new_id("trace"))
    # A span id is NOT invented here. Binding a trace is not the same as
    # opening a span, and minting one anyway gave the first real span a
    # parent that referred to nothing -- so no span was ever parentless, the
    # trace had no root, and the tree could not be assembled from it. Only
    # TraceSpan sets this, by passing it explicitly.
    span = values.pop("span_id", None) or _span_id.get()
    merged = {**current_context(), **values, "trace_id": trace}
    if span:
        merged["span_id"] = str(span)
    return _trace_id.set(trace), _span_id.set(str(span) if span else None), _correlation.set(merged)


def reset_context(tokens: tuple[contextvars.Token, contextvars.Token, contextvars.Token]) -> None:
    _trace_id.reset(tokens[0])
    _span_id.reset(tokens[1])
    _correlation.reset(tokens[2])


@contextmanager
def trace_context(**values: Any) -> Iterator[str]:
    """Bind correlation values for the duration of a block, then restore.

    Every entry point into the system uses this, and that is what makes a
    trace a trace. Toda is queue-driven -- a Slack message becomes an
    `event.route` job, which enqueues `session.dispatch`, which runs the
    agent, which enqueues delivery -- and each hop is a fresh thread picking
    work off the database. Context does not cross that boundary by itself,
    so without an explicit bind at each pickup, one message produced five
    unrelated traces and the thread connecting them was cut at every step.

    Yields the trace id in force, so a caller can persist it alongside
    whatever it just created.
    """
    tokens = bind_context(**values)
    try:
        yield _trace_id.get() or ""
    finally:
        reset_context(tokens)


def current_trace_id() -> str | None:
    """The trace in force right now, if any. Used where a durable record
    (a Task, a Session message) should adopt the ambient trace rather than
    minting a fresh one and orphaning itself from the request that caused
    it."""
    return _trace_id.get()


def _safe(value: Any, *, max_bytes: int = 100_000, key: str = "") -> Any:
    if any(marker in key.lower() for marker in _SECRET_MARKERS):
        return "[REDACTED]"
    if isinstance(value, dict):
        result = {str(k): _safe(v, max_bytes=max_bytes, key=str(k)) for k, v in value.items()}
    elif isinstance(value, (list, tuple)):
        result = [_safe(item, max_bytes=max_bytes) for item in value]
    elif isinstance(value, (str, int, float, bool)) or value is None:
        result = value
    else:
        result = str(value)
    try:
        if len(json.dumps(result, default=str)) > max_bytes:
            return {"truncated": True, "preview": str(result)[:max_bytes], "bytes": len(str(result))}
    except Exception:
        return str(result)[:max_bytes]
    return result


_PAYLOAD_SUPPRESSED = "[payloads disabled]"


class TraceRecorder:
    def __init__(
        self,
        store: Any,
        *,
        enabled: bool = True,
        max_payload_bytes: int = 100_000,
        capture_payloads: bool = True,
    ) -> None:
        self.store = store
        self.enabled = enabled
        self.max_payload_bytes = max_payload_bytes
        # The switch someone reaches for when they do not want tool arguments
        # and model output written to disk. It was a documented setting wired
        # to nothing, which is worse than not offering it: redaction only
        # catches keys *named* like secrets, so a customer's email in a tool
        # argument was persisted whatever this said.
        self.capture_payloads = capture_payloads

    def record(
        self,
        operation: str,
        *,
        component: str | None = None,
        phase: str = "event",
        input: Any = None,
        output: Any = None,
        error: Any = None,
        duration_ms: float | None = None,
        **context: Any,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        merged = {**current_context(), **context}

        def _payload(value: Any) -> Any:
            # Context and errors survive with payloads off: they are how a
            # trace stays navigable and debuggable, and neither carries the
            # bulk content this switch exists to keep off disk.
            if not self.capture_payloads:
                return _PAYLOAD_SUPPRESSED if value is not None else None
            return _safe(value, max_bytes=self.max_payload_bytes)

        event = {
            "id": new_id("tev"),
            "trace_id": merged.pop("trace_id", None) or new_id("trace"),
            "span_id": merged.pop("span_id", None) or new_id("span"),
            "parent_span_id": merged.pop("parent_span_id", None),
            "component": component or operation.split(".", 1)[0],
            "operation": operation,
            "phase": phase,
            "context": _safe(merged, max_bytes=self.max_payload_bytes),
            "input": _payload(input),
            "output": _payload(output),
            "error": _safe(error, max_bytes=self.max_payload_bytes),
            "duration_ms": round(duration_ms, 2) if duration_ms is not None else None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.store.append_trace_event(event)
        except Exception:
            log.exception("trace.persist_failed operation=%s", operation)
        log.info("trace event=%s phase=%s trace_id=%s", operation, phase, event["trace_id"])
        return event

    @contextmanager
    def span(self, operation: str, *, component: str | None = None, input: Any = None, **context: Any) -> Iterator["TraceSpan"]:
        parent = _span_id.get()
        span = TraceSpan(self, operation, component=component, parent_span_id=parent, input=input, context=context)
        span.start()
        try:
            yield span
        except Exception as exc:
            span.fail(exc)
            raise
        else:
            span.complete()


class TraceSpan:
    def __init__(self, recorder: TraceRecorder, operation: str, *, component: str | None, parent_span_id: str | None, input: Any, context: dict[str, Any]) -> None:
        self.recorder = recorder
        self.operation = operation
        self.component = component
        self.parent_span_id = parent_span_id
        self.input = input
        self.context = context
        self.span_id = new_id("span")
        self.started = 0.0
        self.output_value: Any = None
        self.error_value: Any = None

    def start(self) -> None:
        self.started = time.perf_counter()
        tokens = bind_context(span_id=self.span_id, parent_span_id=self.parent_span_id, **self.context)
        self._tokens = tokens
        self.recorder.record(self.operation, component=self.component, phase="started", input=self.input, span_id=self.span_id, parent_span_id=self.parent_span_id, **self.context)

    def output(self, value: Any) -> None:
        self.output_value = value

    def error(self, value: Any) -> None:
        """Mark this span failed without raising.

        Plenty of real failures are caught and turned into a return value
        rather than propagated -- a tool that fails hands back a
        ToolResult(status="failed") so the model can react to it. Those must
        still read as failures in a trace, or the one view meant to show
        what went wrong is the view that hides it.
        """
        self.error_value = value

    def complete(self) -> None:
        if self.error_value is not None:
            self.recorder.record(self.operation, component=self.component, phase="failed", output=self.output_value, error=self.error_value, span_id=self.span_id, parent_span_id=self.parent_span_id, duration_ms=(time.perf_counter() - self.started) * 1000, **self.context)
        else:
            self.recorder.record(self.operation, component=self.component, phase="completed", output=self.output_value, span_id=self.span_id, parent_span_id=self.parent_span_id, duration_ms=(time.perf_counter() - self.started) * 1000, **self.context)
        reset_context(self._tokens)

    def fail(self, exc: BaseException) -> None:
        self.recorder.record(self.operation, component=self.component, phase="failed", error={"type": type(exc).__name__, "message": str(exc)}, span_id=self.span_id, parent_span_id=self.parent_span_id, duration_ms=(time.perf_counter() - self.started) * 1000, **self.context)
        reset_context(self._tokens)
