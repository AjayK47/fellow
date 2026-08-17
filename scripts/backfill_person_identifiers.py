"""Give people who existed before email resolution their email identifier.

`resolve_slack_principal` now registers `slack:{workspace}:{user}` and
`email:{address}` for everyone it sees, so a person who speaks again is
repaired on their next message. Someone who has gone quiet is not -- and until
they have an email identifier, their Slack account and their Gmail/GitHub/
Linear activity stay two unrelated entities, which is the whole reason the
address is the join key.

This finds person entities carrying a `slack_user_id` but no email identifier,
resolves the address from Slack, and links it. Nothing is invented: a person
Slack has no email for is reported and skipped.

    uv run python scripts/backfill_person_identifiers.py            # report
    uv run python scripts/backfill_person_identifiers.py --apply    # link

Requires the `users:read.email` scope. If the workspace was installed before
that scope was added, reinstall via /slack/install first -- otherwise Slack
returns profiles with no address and this correctly reports nothing to do.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from toda.config import settings  # noqa: E402
from toda.services.container import TodaApp  # noqa: E402


def _slack_email(token: str, source_user_id: str) -> str:
    try:
        response = httpx.get(
            "https://slack.com/api/users.info",
            headers={"Authorization": f"Bearer {token}"},
            params={"user": source_user_id},
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        print(f"    lookup failed: {exc}")
        return ""
    if not payload.get("ok"):
        print(f"    slack error: {payload.get('error')}")
        return ""
    profile = ((payload.get("user") or {}).get("profile")) or {}
    return str(profile.get("email") or "")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(settings.db_path))
    parser.add_argument("--organization", default=settings.default_org_id)
    parser.add_argument(
        "--apply", action="store_true", help="Write the links (default: report only)."
    )
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"No database at {args.db}. Pass --db PATH.")
        return

    brain = TodaApp(db_path=Path(args.db))
    try:
        organization_id = args.organization
        people = [
            entity
            for entity in brain.store.list_entities(organization_id)
            if entity.get("entity_type") == "person"
            and (entity.get("attributes") or {}).get("slack_user_id")
        ]
        if not people:
            print("No Slack-derived person entities found. Nothing to do.")
            return

        pending = []
        for person in people:
            existing = [
                item
                for item in brain.reality.list_identifiers(organization_id, person["id"])
                if item["namespace"] == "email"
            ]
            if not existing:
                pending.append(person)

        print(f"{len(people)} Slack people, {len(pending)} without an email identifier.\n")
        if not pending:
            return

        linked = skipped = 0
        for person in pending:
            attributes = person.get("attributes") or {}
            workspace_id = str(attributes.get("workspace_id") or "")
            source_user_id = str(attributes.get("slack_user_id") or "")
            print(f"  {person['id']}  ({person.get('name')})")

            installation = brain.store.slack_installation_by_workspace(workspace_id)
            token = (installation or {}).get("bot_token") or settings.slack_bot_token
            if not token:
                print("    no bot token for this workspace; skipped")
                skipped += 1
                continue

            email = attributes.get("email") or _slack_email(token, source_user_id)
            if not email:
                print("    slack has no email for this user; skipped")
                skipped += 1
                continue

            print(f"    -> {email}")
            if not args.apply:
                continue

            result = brain.reality.link_identifier_or_propose(
                organization_id=organization_id,
                entity_id=person["id"],
                namespace="email",
                value=email,
                confidence=1.0,
                verified=True,
                label=person.get("name") or email,
            )
            if result is None:
                # Already owned by a different entity -- these are the two
                # halves of one human, and merging them is a judgement call
                # for a person, not a backfill.
                print("    address belongs to another entity; filed for review")
                skipped += 1
                continue
            entity = brain.store.get_entity(person["id"])
            entity["attributes"] = {**(entity.get("attributes") or {}), "email": email}
            brain.graph.upsert_entity(entity)
            linked += 1

        print()
        if args.apply:
            print(f"Linked {linked}, skipped {skipped}.")
        else:
            print("Report only. Re-run with --apply to write these links.")
    finally:
        brain.close()


if __name__ == "__main__":
    main()
