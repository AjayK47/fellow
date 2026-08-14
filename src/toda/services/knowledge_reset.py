"""Clear what Toda has learned, keeping who it is and what it can do.

Deliberately expressed as a *keep* list rather than a clear list. A clear
list silently misses every table added after it was written, which is
exactly the drift already visible in retention.py -- its tenant-table set
omits sixteen org-owned tables, so `delete_organization` reports
"recoverable: false" while leaving raw Slack text and plaintext
credentials behind. Inverting the default means a new table is cleared
unless someone deliberately preserves it: the safe failure direction for
an operation whose entire purpose is "forget everything".
"""

from __future__ import annotations

from typing import Any

# Identity, access, configuration, and capability. Everything Toda *is*
# rather than anything it has learned or done.
_KEEP: frozenset[str] = frozenset(
    {
        # the tenant and who may reach it
        "organizations",
        "organization_members",
        "principal_memberships",
        "roles",
        "scopes",
        # console login
        "dashboard_users",
        "dashboard_sessions",
        "dashboard_login_attempts",
        "dashboard_invitations",
        "dashboard_access_requests",
        # connected systems and their credentials
        "slack_installations",
        "identity_mappings",
        "mcp_servers",
        "connector_accounts",
        # what Toda is allowed and able to do
        "skills",
        "skill_versions",
        "skill_packages",
        "capabilities",
        "policy_versions",
        "tool_policies",
        "channel_policies",
        "action_preconditions",
        # operator-authored, not learned
        "automations",
        # index geometry: dropping this forces a needless rebuild
        "vector_index_meta",
        # persistent per-workspace folder mapping for on-disk task files
        "workspaces",
    }
)

# FTS5 and vec0 shadow tables are managed through their virtual parent;
# deleting from a shadow directly corrupts the index.
_SHADOW_SUFFIXES = (
    "_config", "_content", "_data", "_docsize", "_idx",
    "_chunks", "_info", "_rowids",
)
_SHADOW_PREFIXES = ("chunks_fts", "episodes_fts", "memories_fts", "tasks_fts",
                    "vec_chunks", "vec_episodes", "vec_memories", "vec_tasks")


def _is_shadow(name: str) -> bool:
    for prefix in _SHADOW_PREFIXES:
        if name.startswith(prefix) and name != prefix:
            rest = name[len(prefix):]
            if rest.startswith("_vector_chunks") or rest in _SHADOW_SUFFIXES:
                return True
    return False


def clear_knowledge(store: Any) -> dict[str, Any]:
    """Empty every table that holds observed or derived state.

    Runs through BrainStore rather than a bare sqlite3 connection because
    the vec0 virtual tables are only reachable with the sqlite-vec
    extension loaded -- a plain connection fails them with "no such module:
    vec0" and silently leaves the vectors behind.
    """
    cleared: dict[str, int] = {}
    failed: dict[str, str] = {}
    with store.cursor() as cur:
        names = [
            row[0]
            for row in cur.execute(
                "select name from sqlite_master where type='table' "
                "and name not like 'sqlite_%'"
            ).fetchall()
        ]
        for name in sorted(names):
            if name in _KEEP or _is_shadow(name):
                continue
            try:
                before = cur.execute(f"select count(*) from [{name}]").fetchone()[0]
            except Exception:
                before = None
            try:
                cur.execute(f"delete from [{name}]")
            except Exception as exc:  # pragma: no cover - provider specific
                failed[name] = str(exc)[:120]
                continue
            if before:
                cleared[name] = before
    return {
        "cleared": cleared,
        "failed": failed,
        "rows": sum(cleared.values()),
        "kept": sorted(_KEEP),
    }
