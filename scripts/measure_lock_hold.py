"""How long does a root Task hold the Session Manager lock before it pauses?

One Session per room means one Manager lock per room, and the lock is only
released at a pause point (delegation, approval, human input) or completion.
If a root Task typically delegates within a turn or two, a second person in
the channel waits a couple of seconds. If root Tasks routinely grind many
tool calls inline, they block everyone else in the room and per-root lock
scoping becomes necessary.

This reads `task_turns` and `tasks` -- both already recorded -- and reports
time-to-first-pause per root Task, so the question is answered from data
rather than argument.

    uv run python scripts/measure_lock_hold.py [--db PATH] [--days N]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from toda.config import settings  # noqa: E402

PAUSE_STATES = ("waiting_for_children", "waiting_for_approval", "waiting_for_human")
TERMINAL_STATES = ("completed", "failed", "cancelled")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(settings.db_path))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument(
        "--all-destinations",
        action="store_true",
        help="Include DM and internal root Tasks, not only channel ones.",
    )
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"No database at {args.db}. Pass --db PATH.")
        return

    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    connection = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row

    roots = connection.execute(
        """SELECT id, state, created_at, updated_at, payload
        FROM tasks
        WHERE parent_task_id IS NULL AND created_at >= ?""",
        (since,),
    ).fetchall()

    samples: list[tuple[str, float, int, str]] = []
    seen_destinations: Counter[str] = Counter()
    for root in roots:
        payload = json.loads(root["payload"]) if root["payload"] else {}
        destination = ((payload.get("envelope") or {}).get("destination") or {})
        destination_type = str(destination.get("type") or "unknown")
        seen_destinations[destination_type] += 1
        if not args.all_destinations and destination_type not in {
            "slack_channel",
            "private_channel",
        }:
            continue

        started = _parse(root["created_at"])
        if not started:
            continue

        # The first durable pause or terminal transition ends the lock hold.
        # `events` on the Task record carries every transition with its time.
        released_at: datetime | None = None
        turns_before_release = 0
        for event in payload.get("events") or []:
            state = str(event.get("state") or event.get("type") or "")
            moment = _parse(event.get("at"))
            if not moment:
                continue
            if any(marker in state for marker in PAUSE_STATES + TERMINAL_STATES):
                released_at = moment
                break
        if released_at is None:
            released_at = _parse(root["updated_at"])
        if released_at is None:
            continue

        turns_before_release = connection.execute(
            "SELECT COUNT(*) AS n FROM task_turns WHERE task_id = ? AND created_at <= ?",
            (root["id"], released_at.isoformat()),
        ).fetchone()["n"]

        held = max(0.0, (released_at - started).total_seconds())
        samples.append((root["id"], held, turns_before_release, root["state"]))

    connection.close()

    if not samples:
        scope = "root" if args.all_destinations else "channel root"
        print(f"No {scope} Tasks in the last {args.days} days at {args.db}.")
        print(f"Root Tasks seen: {len(roots)}")
        if seen_destinations:
            print("By destination: " + ", ".join(
                f"{name}={count}" for name, count in seen_destinations.most_common()
            ))
            print("Try --all-destinations, or widen --days.")
        return

    held = [item[1] for item in samples]
    turns = [float(item[2]) for item in samples]

    scope = "Root" if args.all_destinations else "Channel root"
    print(f"{scope} Tasks in the last {args.days} days: {len(samples)}")
    print("By destination: " + ", ".join(
        f"{name}={count}" for name, count in seen_destinations.most_common()
    ) + "\n")
    print("Lock hold before first pause or completion")
    for label, fraction in (("p50", 0.5), ("p90", 0.9), ("p99", 0.99), ("max", 1.0)):
        print(f"  {label:>4}  {_percentile(held, fraction):8.1f}s"
              f"   {_percentile(turns, fraction):5.1f} turns")

    over_30 = sum(1 for value in held if value > 30)
    print(f"\n  held > 30s: {over_30} / {len(samples)}"
          f"  ({100 * over_30 / len(samples):.0f}%)")

    print("\nSlowest 10:")
    for task_id, seconds, turn_count, state in sorted(
        samples, key=lambda item: -item[1]
    )[:10]:
        print(f"  {task_id}  {seconds:8.1f}s  {turn_count:3d} turns  {state}")

    print("\nReading:")
    print("  p90 under ~30s      → one Session per channel is fine as-is.")
    print("  p90 in the minutes  → Managers grind inline; add per-root lock")
    print("                        scoping before channels get busy.")


if __name__ == "__main__":
    main()
