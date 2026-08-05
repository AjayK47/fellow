"""Shared transient-vs-final failure classification.

Used by Session Manager and child Task dispatch so transient failures follow
one retry policy.
"""

from __future__ import annotations

_RETRYABLE_ERROR_MARKERS = (
    "timeout",
    "timed out",
    "connection",
    "temporarily unavailable",
    "rate limit",
    "429",
    "500 internal server",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway",
    "model returned no choices",
)


def is_retryable_task_error(error: str) -> bool:
    lower = (error or "").lower()
    return any(marker in lower for marker in _RETRYABLE_ERROR_MARKERS)
