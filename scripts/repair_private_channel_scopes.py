"""Find Slack channel scopes wrongly stored as organization-public.

Before the `channel_type="group"` fix, `SlackGateway.normalize()` classified
every private channel as `organization_public`, so its evidence was readable
by anyone in the workspace. `_ensure_scope()` now self-heals a mislabelled
scope on the channel's next message, but a channel that has gone quiet stays
wrong until something corrects it -- and you usually want to know the blast
radius before waiting for that.

This reports every candidate and, with --apply, narrows them in place. It
never widens a scope, and it preserves everyone already recorded as a member
so the room's own participants keep access.

    uv run python scripts/repair_private_channel_scopes.py            # report
    uv run python scripts/repair_private_channel_scopes.py --apply    # fix
    uv run python scripts/repair_private_channel_scopes.py --apply \\
        --channel C0BP95K48V6 --channel C0ABC123XYZ                   # targeted
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from toda.config import settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(settings.db_path))
    parser.add_argument(
        "--apply", action="store_true", help="Write the change (default: report only)."
    )
    parser.add_argument(
        "--channel",
        action="append",
        default=[],
        help="Only repair these channel ids. Repeatable. Omit to list all candidates.",
    )
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"No database at {args.db}. Pass --db PATH.")
        return

    mode = "" if args.apply else "?mode=ro"
    connection = sqlite3.connect(f"file:{args.db}{mode}", uri=True)
    connection.row_factory = sqlite3.Row

    scopes = [
        (row["id"], json.loads(row["payload"]))
        for row in connection.execute("SELECT id, payload FROM scopes")
    ]
    targets = set(args.channel)

    candidates = [
        (scope_id, scope)
        for scope_id, scope in scopes
        if scope.get("channel_id")
        and bool(scope.get("is_public"))
        and (not targets or scope["channel_id"] in targets)
    ]

    if not candidates:
        print("No public Slack channel scopes found. Nothing to review.")
        connection.close()
        return

    print(f"Public Slack channel scopes in {args.db}:\n")
    for scope_id, scope in candidates:
        # `sources` keeps scope_id inside its JSON payload, not as a column.
        sources = sum(
            1
            for row in connection.execute("SELECT payload FROM sources")
            if json.loads(row["payload"]).get("scope_id") == scope_id
        )
        chunks = connection.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE scope_id = ?", (scope_id,)
        ).fetchone()["n"]
        memories = connection.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE scope_id = ?", (scope_id,)
        ).fetchone()["n"]
        members = [
            row["principal_id"]
            for row in connection.execute(
                "SELECT principal_id FROM principal_memberships WHERE scope_id = ?",
                (scope_id,),
            )
        ]
        print(f"  channel {scope['channel_id']}  ({scope_id})")
        print(f"    evidence readable org-wide: {sources} sources, "
              f"{chunks} chunks, {memories} memories")
        print(f"    known participants: {len(members)}")

    print()
    if not args.apply:
        print("Report only. These are ALL public channel scopes -- a genuinely")
        print("public channel belongs here. Confirm which are private in Slack,")
        print("then re-run with --apply --channel <id> for each private one.")
        connection.close()
        return

    if not targets:
        print("Refusing to narrow every channel scope at once: some are")
        print("legitimately public. Re-run with --channel <id> per private room.")
        connection.close()
        return

    for scope_id, scope in candidates:
        members = [
            row["principal_id"]
            for row in connection.execute(
                "SELECT principal_id FROM principal_memberships WHERE scope_id = ?",
                (scope_id,),
            )
        ]
        scope["kind"] = "private_channel"
        scope["is_public"] = False
        scope["principal_ids"] = sorted(
            set(scope.get("principal_ids") or []) | set(members)
        )
        # `scopes` stores everything in the JSON payload; there are no
        # denormalised kind/is_public columns to keep in step.
        connection.execute(
            "UPDATE scopes SET payload = ? WHERE id = ?",
            (json.dumps(scope), scope_id),
        )
        print(f"  narrowed {scope_id} -> private_channel "
              f"({len(scope['principal_ids'])} members preserved)")

    connection.commit()
    connection.close()
    print("\nDone. Verify with a read-only re-run.")


if __name__ == "__main__":
    main()
