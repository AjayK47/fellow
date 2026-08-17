"""Deterministic detection and redaction of secrets/credentials in text.

Applied before any content is persisted as a `Source` (and therefore before
memory extraction and reality-engine ingestion, both of which read from the
already-stored source content) — never after. Only the *category* of what
was redacted is ever recorded; the matched value itself is discarded and
never logged, audited, or returned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Ordered so a match consumed by an earlier, more specific pattern (e.g. a
# bearer token) can't also be re-matched and double-counted by a later,
# broader one (e.g. a bare JWT) — each pattern runs against the output of
# the ones before it.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key_block", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----[\s\S]+?"
        r"-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    )),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9-]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("stripe_key", re.compile(r"\bsk_live_[0-9a-zA-Z]{16,}\b")),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-_.]{20,}\b")),
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"
    )),
    ("connection_string_credential", re.compile(
        r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s:/@]+:[^\s:/@]+@[^\s]+"
    )),
    ("key_value_secret", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|password|passwd|token|credential)\b"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+]{8,}['\"]?"
    )),
]


@dataclass
class RedactionResult:
    text: str
    categories: list[str] = field(default_factory=list)

    @property
    def redacted(self) -> bool:
        return bool(self.categories)


def scan(text: str) -> list[str]:
    """Return the distinct secret categories detected in *text*, without exposing matches."""
    if not text:
        return []
    return [category for category, pattern in _PATTERNS if pattern.search(text)]


def redact(text: str) -> RedactionResult:
    """Replace every detected secret with a category placeholder.

    Never returns or stores the original matched value — only the name of
    the pattern that matched, for audit purposes.
    """
    if not text:
        return RedactionResult(text=text, categories=[])
    seen: dict[str, None] = {}
    result = text
    for category, pattern in _PATTERNS:
        def _replace(match: re.Match[str], _category: str = category) -> str:
            seen.setdefault(_category, None)
            return f"[REDACTED:{_category}]"

        result = pattern.sub(_replace, result)
    return RedactionResult(text=result, categories=list(seen.keys()))
