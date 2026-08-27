"""W3C context propagation helpers.

These functions intentionally delegate to OpenTelemetry's context and
propagator implementations. Toda does not keep a parallel ContextVar trace
state.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Link
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator


_TRACE_CONTEXT = TraceContextTextMapPropagator()
_W3C_KEYS = frozenset({"traceparent", "tracestate"})


def _trace_headers(carrier: dict[str, str] | None) -> dict[str, str]:
    """Keep propagation limited to W3C trace context.

    The default OpenTelemetry composite propagator also carries baggage. Toda
    correlation is local context, not baggage, so accepting or emitting an
    arbitrary incoming ``baggage`` header would disclose internal identifiers
    to every instrumented third-party request.
    """
    return {
        str(key).lower(): str(value)
        for key, value in (carrier or {}).items()
        if str(key).lower() in _W3C_KEYS
    }


def inject_carrier() -> dict[str, str]:
    carrier: dict[str, str] = {}
    _TRACE_CONTEXT.inject(carrier)
    return carrier


def current_otel_trace_id() -> str | None:
    """Return the active valid OTel trace ID, never a Toda-generated ID."""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return f"{span_context.trace_id:032x}"


def extract_context(carrier: dict[str, str] | None) -> Any:
    return _TRACE_CONTEXT.extract(_trace_headers(carrier))


@contextmanager
def use_carrier(carrier: dict[str, str] | None) -> Iterator[Any]:
    ctx = extract_context(carrier)
    token = otel_context.attach(ctx)
    try:
        yield ctx
    finally:
        otel_context.detach(token)


def link_from_carrier(carrier: dict[str, str] | None) -> Link | None:
    """Create a span link from a valid W3C carrier when possible."""

    ctx = extract_context(carrier)
    span = trace.get_current_span(ctx)
    span_context = span.get_span_context()
    if span_context.is_valid:
        return Link(span_context)
    # The public propagator does not expose a parsed SpanContext. The carrier
    # itself is still used for parent extraction; this fallback is intentionally
    # absent rather than inventing IDs that OTel did not validate.
    return None


def carrier_has_context(carrier: dict[str, str] | None) -> bool:
    ctx = extract_context(carrier)
    span = trace.get_current_span(ctx)
    return span.get_span_context().is_valid
