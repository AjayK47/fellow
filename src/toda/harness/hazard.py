"""Deterministic backstop for the obviously-catastrophic.

Adapted from OpenMausBot's `auto-approve.ts` (MIT), whose framing is worth
repeating because it is the honest one:

    The guard is deliberately tiny and literal. It is NOT a security
    boundary (an agent set on damage has a thousand spellings for `rm`);
    it is a "you probably didn't mean to hand THIS one over unattended"
    backstop for the obvious catastrophes. Real containment is the
    sandbox.

Toda has no sandbox for `run_code` or Pi -- both run as ordinary processes
under Toda's own account, verified reading `/etc/hosts`, listing `$HOME`
and opening sockets. So this is currently the *only* thing between an
injected instruction and the host, which is precisely why it must not be
mistaken for sufficient. It buys the obvious cases and nothing more.

What it does buy: these patterns force a human decision rather than letting
the auxiliary action reviewer wave them through, which under the default
`autonomous` policy mode is what would otherwise happen.
"""

from __future__ import annotations

import re
from pathlib import Path

# Hard to reverse, and rarely what anyone meant to delegate unattended.
_DESTRUCTIVE = (
    re.compile(r"\brm\s+(-[a-z]*\s+)*-[a-z]*[rf]", re.I),
    re.compile(r"\bmkfs\b|\bdiskutil\s+erase|\bdd\b[^|]{0,80}\bof=/dev/", re.I),
    re.compile(r"\bshutdown\b|\breboot\b|\bhalt\b", re.I),
    re.compile(r":\(\)\s*\{.*\}\s*;?\s*:"),  # fork bomb
    re.compile(r"\bgit\s+push\s+[^|]*--force(-with-lease)?\b|\bgit\s+reset\s+--hard\b", re.I),
    re.compile(r"\bDROP\s+(TABLE|DATABASE)\b|\bTRUNCATE\s+TABLE\b", re.I),
    re.compile(r"\bsudo\s+rm\b|\bchmod\s+-R\s+777\s+/", re.I),
    re.compile(r"\bshutil\.rmtree\s*\(\s*[\"']?/", re.I),
)

# Quiet, permanent and unrecoverable rather than loud and destructive. An
# agent reading credentials does not look like an incident until much later.
_SENSITIVE = (
    re.compile(r"(^|[\s/\"'])\.env(\.|$|[\"'\s])", re.I),
    re.compile(r"\.ssh/|id_rsa|id_ed25519|authorized_keys", re.I),
    re.compile(r"\.aws/credentials|\.netrc|\.npmrc|\.pypirc|\.docker/config\.json", re.I),
    re.compile(r"security\s+find-(generic|internet)-password|\bkeychain\b", re.I),
    re.compile(r"\bcredentials?\.json\b|\bserviceaccount\b", re.I),
    re.compile(r"\bgcloud\s+auth\b|\bkubectl\s+config\s+view\b", re.I),
)

# Toda's own state. Not in the upstream list, because OpenMausBot has no
# equivalent: this one SQLite file holds every memory, transcript, artifact,
# approval and connector credential the organization has. A script reading
# it exfiltrates the whole company at once, and it sits at a predictable
# path the model can guess. `config.json` is the same argument for the
# provider keys.
_TODA_STATE = (
    re.compile(r"\bbrain\.db\b|\bbrain\.db-(wal|shm)\b", re.I),
    re.compile(r"\bconfig\.json\b", re.I),
    re.compile(r"Application Support/Toda|\.local/share/toda", re.I),
    re.compile(r"\bsqlite3\s*\.\s*connect\b", re.I),
)


def looks_destructive(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _DESTRUCTIVE)


def looks_sensitive(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _SENSITIVE)


def touches_toda_state(text: str, *, data_dir: Path | str | None = None) -> bool:
    """True when the text reaches for Toda's own database or configuration."""
    if any(pattern.search(text or "") for pattern in _TODA_STATE):
        return True
    if data_dir and str(data_dir) in (text or ""):
        return True
    return False


def hazard_reason(text: str, *, data_dir: Path | str | None = None) -> str | None:
    """One human-readable reason, or None when nothing obvious is matched.

    Ordered most-serious first so an approval preview names the worst thing
    it found rather than the first alphabetically.
    """
    if touches_toda_state(text, data_dir=data_dir):
        return (
            "reaches Toda's own database or configuration, which holds every "
            "memory, transcript and connector credential"
        )
    if looks_sensitive(text):
        return "reads credentials or private keys"
    if looks_destructive(text):
        return "contains an irreversible or destructive command"
    return None
