#!/usr/bin/env python3
"""Clear what Toda has learned, keeping who it is and what it can do.

    python scripts/clear_knowledge.py --yes

Forgets memories, entities, the graph, sources, chunks, facts, cases,
conversations, sessions, tasks, jobs, and audit history. Keeps the
organization, your console login, the Slack and MCP connections, skills,
policies, and operator-authored automations -- so it forgets without
signing you out or forcing a reinstall.

A maintenance tool rather than a product command: `toda reset --yes` (the
shipped one) deletes the database outright, which is a different and
blunter thing.

Deliberately expressed as a *keep* list rather than a clear list. A clear
list silently misses every table added after it was written, which is
exactly the drift already visible in src/toda/services/retention.py -- its
tenant-table set omits sixteen org-owned tables, so `delete_organization`
reports "recoverable: false" while leaving raw Slack text and plaintext
credentials behind. Inverting the default means a new table is cleared
unless someone deliberately preserves it: the safe failure direction for
an operation whose entire purpose is "forget everything".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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


def preview(store: Any) -> dict[str, int]:
    """Row counts for everything `clear_knowledge` would empty."""
    counts: dict[str, int] = {}
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
                count = cur.execute(f"select count(*) from [{name}]").fetchone()[0]
            except Exception:
                continue
            if count:
                counts[name] = count
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--yes", action="store_true", help="actually clear (default is a dry run)"
    )
    parser.add_argument(
        "--db", default=None, help="database path (defaults to the configured one)"
    )
    args = parser.parse_args()

    from toda.config import settings
    from toda.runtime_owner import DatabaseOwner
    from toda.services.container import reset_app

    db_path = Path(args.db).expanduser() if args.db else settings.storage.db_path
    if not db_path.exists():
        print(f"No database at {db_path}")
        raise SystemExit(1)

    owner = DatabaseOwner(db_path)
    owner.acquire()
    try:
        brain = reset_app(db_path)
        try:
            if not args.yes:
                counts = preview(brain.store)
                total = sum(counts.values())
                for name, count in sorted(counts.items(), key=lambda i: -i[1]):
                    print(f"  {count:8d}  {name}")
                print(f"\nWould clear {total} row(s) across {len(counts)} table(s).")
                print(f"Would keep {len(_KEEP)} table(s): organization, login, "
                      f"connections, skills, policies.")
                print("\nRe-run with --yes to apply.")
                return
            result = clear_knowledge(brain.store)
        finally:
            brain.close()
    finally:
        owner.release()

    for name, count in sorted(result["cleared"].items(), key=lambda i: -i[1]):
        print(f"  {count:8d}  {name}")
    print(f"\nCleared {result['rows']} row(s) across {len(result['cleared'])} table(s).")
    if result["failed"]:
        print("\nCould not clear:")
        for name, error in result["failed"].items():
            print(f"  {name}: {error}")


if __name__ == "__main__":
    main()
