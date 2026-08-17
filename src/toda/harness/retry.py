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


# Failures that cannot succeed on a retry, whatever the attempt ceiling
# says. Deliberately a *positive* list rather than "not retryable": an
# unfamiliar error is far more likely to be a transient blip than a
# permanent rejection, and dead-lettering it would silently lose real work.
#
# What is here is the class of failure where the request itself is the
# problem -- a malformed prompt, a schema the provider will not accept.
# Sending it again produces the identical rejection, at a full model round
# trip per lap. Observed live: one tool result carrying JSON-Schema `$defs`
# made every later request in a Task 400, and `session.dispatch`'s
# 100-attempt ceiling turned that into minutes of looping.
_PERMANENT_ERROR_MARKERS = (
    "invalid_prompt",
    "invalid_request",
    "invalid_argument",
    "unprocessable",
    "does not match to a display_name",
    "context_window_exceeded",
    "422",
)


def is_permanent_task_error(error: str) -> bool:
    lower = (error or "").lower()
    if is_retryable_task_error(lower):
        # A rate limit that happens to mention "invalid" is still a rate
        # limit; retryable always wins.
        return False
    return any(marker in lower for marker in _PERMANENT_ERROR_MARKERS)
