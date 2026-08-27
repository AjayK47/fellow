"""Operator logging helpers.

Execution telemetry lives in :mod:`toda.telemetry`. This module only keeps
the stdlib JSON log formatter so logs and spans cannot diverge into two
correlation systems.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from opentelemetry import trace

from toda.services.redaction import redact


def _current_span_context() -> dict[str, str]:
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return {}
    return {
        "trace_id": f"{context.trace_id:032x}",
        "span_id": f"{context.span_id:016x}",
    }


class JsonLogFormatter(logging.Formatter):
    """Small stdlib-compatible JSON formatter for operator logs."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **_current_span_context(),
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


def _safe(value: Any, *, max_bytes: int = 100_000, key: str = "") -> Any:
    if any(marker in key.lower() for marker in (
        "token", "secret", "password", "authorization", "api_key", "apikey", "cookie",
    )):
        return "[REDACTED]"
    if isinstance(value, dict):
        result = {str(k): _safe(v, max_bytes=max_bytes, key=str(k)) for k, v in value.items()}
    elif isinstance(value, (list, tuple)):
        result = [_safe(item, max_bytes=max_bytes) for item in value]
    elif isinstance(value, str):
        result = redact(value).text
    elif isinstance(value, (int, float, bool)) or value is None:
        result = value
    else:
        result = str(value)
    try:
        encoded = json.dumps(result, default=str)
        if len(encoded.encode("utf-8")) > max_bytes:
            return {"truncated": True, "bytes": len(encoded.encode("utf-8"))}
    except Exception:
        return str(result)[:max_bytes]
    return result
