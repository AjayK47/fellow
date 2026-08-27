"""Local completed-span exporter for the self-hosted console."""

from __future__ import annotations

from typing import Any, Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult, SpanExporter


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _context_id(value: Any, width: int) -> str | None:
    if value is None:
        return None
    identifier = getattr(value, "__int__", None)
    if identifier is None:
        return None
    try:
        integer = int(value)
    except (TypeError, ValueError):
        return None
    return f"{integer:0{width}x}"


class SQLiteSpanExporter(SpanExporter):
    """Synchronous local exporter so finished spans are immediately queryable."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self._shutdown = False

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._shutdown:
            return SpanExportResult.FAILURE
        try:
            for span in spans:
                self.store.append_otel_span(self._convert(span))
            return SpanExportResult.SUCCESS
        except Exception:
            # Telemetry must never break the operation being observed.
            return SpanExportResult.FAILURE

    def _convert(self, span: ReadableSpan) -> dict[str, Any]:
        context = span.context
        parent = span.parent
        resource = _jsonable(dict(span.resource.attributes))
        attrs = _jsonable(dict(span.attributes or {}))
        events = [
            {
                "name": event.name,
                "timestamp": event.timestamp,
                "attributes": _jsonable(dict(event.attributes or {})),
            }
            for event in span.events
        ]
        links = [
            {
                "trace_id": _context_id(link.context.trace_id, 32),
                "span_id": _context_id(link.context.span_id, 16),
                "trace_state": str(link.context.trace_state),
                "attributes": _jsonable(dict(link.attributes or {})),
            }
            for link in span.links
        ]
        status = span.status
        attributes = {
            str(key): value for key, value in attrs.items()
        }
        return {
            "id": f"otel_{_context_id(context.span_id, 16)}_{span.start_time}",
            "organization_id": attributes.get("toda.organization.id", ""),
            "trace_id": _context_id(context.trace_id, 32),
            "span_id": _context_id(context.span_id, 16),
            "parent_span_id": _context_id(parent.span_id, 16) if parent else None,
            "name": span.name,
            "kind": getattr(span.kind, "name", str(span.kind)),
            "start_time_ns": span.start_time,
            "end_time_ns": span.end_time,
            "duration_ms": ((span.end_time - span.start_time) / 1_000_000)
            if span.end_time is not None and span.start_time is not None
            else None,
            "status_code": getattr(status.status_code, "name", str(status.status_code)),
            "status_description": status.description,
            "resource_attributes": resource,
            "attributes": attributes,
            "events": events,
            "links": links,
            "scope_name": getattr(span.instrumentation_scope, "name", ""),
            "scope_version": getattr(span.instrumentation_scope, "version", None),
            "task_id": attributes.get("toda.task.id"),
            "root_task_id": attributes.get("toda.root_task.id"),
            "session_id": attributes.get("toda.session.id"),
            "job_id": attributes.get("toda.job.id"),
        }

    def shutdown(self) -> None:
        self._shutdown = True

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True
