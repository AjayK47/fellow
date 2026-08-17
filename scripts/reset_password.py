#!/usr/bin/env python3
"""Set a dashboard account's password.

    python scripts/reset_password.py --list
    python scripts/reset_password.py --email you@example.com
    python scripts/reset_password.py --email you@example.com --password 'chosen one'

There is no self-service reset and deliberately so: the console has no mail
sender, and a single-tenant local install has no third party to verify a
reset request against. Local filesystem access to the database is the
credential here, which is the same trust boundary `toda init` already uses
when it prints the first owner password to a terminal.

Without this, an operator who forgot the password set during the old setup
wizard had no way back in short of deleting the database, since `toda init`
refuses to run once an owner exists.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", default=None, help="account to update")
    parser.add_argument(
        "--password",
        default=None,
        help="new password (a strong one is generated and printed when omitted)",
    )
    parser.add_argument(
        "--list", action="store_true", help="show the accounts on this installation"
    )
    args = parser.parse_args()

    from toda.config import settings
    from toda.domain.models import utcnow
    from toda.runtime_owner import DatabaseOwner
    from toda.services.bootstrap import generate_password
    from toda.services.dashboard_auth import DashboardAuthService
    from toda.storage.db import BrainStore

    db_path = settings.storage.db_path
    if not db_path.exists():
        print(f"No database at {db_path}")
        raise SystemExit(1)

    # Listing is read-only, so it must not require stopping the gateway --
    # needing to take the runtime down to find out which account to reset is
    # a poor way to be locked out.
    import sqlite3

    read = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = read.execute(
            "select id, email, name, status from dashboard_users"
        ).fetchall()
    finally:
        read.close()
    accounts = [{"id": r[0], "email": r[1], "name": r[2], "status": r[3]} for r in rows]

    if args.list or not args.email:
        if not accounts:
            print("No dashboard accounts. Run `toda init` to create the owner.")
            return
        print("accounts on this installation:")
        for account in accounts:
            print(f"  {account['email']:<34} {account['name']:<18} {account['status']}")
        if not args.email:
            print("\nRe-run with --email <address> to set a password.")
        return

    target = next(
        (a for a in accounts if a["email"].lower() == args.email.lower()), None
    )
    if target is None:
        print(f"No account for {args.email}. Known: "
              f"{', '.join(a['email'] for a in accounts) or '(none)'}")
        raise SystemExit(1)

    owner = DatabaseOwner(db_path)
    try:
        owner.acquire()
    except Exception:
        print("Toda is running and owns the database. Stop the gateway, then "
              "re-run this command.")
        raise SystemExit(1) from None
    try:
        store = BrainStore(db_path)
        try:
            secret = args.password or generate_password()
            # Reuse the service's own hasher so the stored format cannot
            # drift from what authenticate() expects.
            DashboardAuthService._validate_password(secret)
            hashed = DashboardAuthService._hash_password(secret)
            with store.cursor() as cur:
                cur.execute(
                    "update dashboard_users set password_hash = ?, updated_at = ? "
                    "where id = ?",
                    (hashed, utcnow().isoformat(), target["id"]),
                )
                # Existing sessions were issued against the old password.
                cur.execute(
                    "delete from dashboard_sessions where user_id = ?", (target["id"],)
                )
        finally:
            store.close()
    finally:
        owner.release()

    print(f"Password updated for {target['email']}")
    if args.password is None:
        print(f"  password: {secret}   (shown once)")
    print(f"  sign in : http://127.0.0.1:{settings.server.port}")
    print("  existing sessions were revoked")


if __name__ == "__main__":
    main()
