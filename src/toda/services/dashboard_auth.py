"""Self-hosted dashboard identity, admission, and revocable sessions."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from toda.domain.models import new_id
from toda.storage.db import BrainStore


ROLES = {"owner", "admin", "member"}
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(email: str) -> str:
    normalized = email.strip().casefold()
    if len(normalized) > 254 or not _EMAIL.fullmatch(normalized):
        raise ValueError("Enter a valid email address")
    return normalized


class DashboardAuthService:
    def __init__(self, store: BrainStore) -> None:
        self.store = store

    @staticmethod
    def _hash_password(password: str) -> str:
        salt = secrets.token_bytes(16)
        derived = hashlib.scrypt(
            password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32
        )
        return f"scrypt$16384$8$1${salt.hex()}${derived.hex()}"

    @staticmethod
    def _password_matches(encoded: str, password: str) -> bool:
        try:
            scheme, n, r, p, salt, expected = encoded.split("$")
            if scheme != "scrypt":
                return False
            derived = hashlib.scrypt(
                password.encode(),
                salt=bytes.fromhex(salt),
                n=int(n),
                r=int(r),
                p=int(p),
                dklen=len(bytes.fromhex(expected)),
            )
            return secrets.compare_digest(derived.hex(), expected)
        except (ValueError, TypeError):
            return False

    @property
    def needs_owner(self) -> bool:
        return self.store.dashboard_owner_count() == 0

    @staticmethod
    def _validate_password(password: str) -> None:
        if len(password) < 12:
            raise ValueError("Password must be at least 12 characters")
        if len(password) > 1024:
            raise ValueError("Password is too long")

    @staticmethod
    def _public_user(user: dict[str, Any]) -> dict[str, Any]:
        return {key: user[key] for key in ("id", "email", "name", "status")}

    def create_owner(
        self, *, organization_id: str, email: str, name: str, password: str
    ) -> dict[str, Any]:
        if not self.needs_owner:
            raise ValueError("The installation owner already exists")
        organizations = self.store.list_organizations()
        if len(organizations) != 1 or organizations[0]["id"] != organization_id:
            raise ValueError("The owner must belong to this installation's organization")
        if not name.strip():
            raise ValueError("Name is required")
        self._validate_password(password)
        timestamp = utcnow().isoformat()
        user = {
            "id": new_id("user"),
            "email": normalize_email(email),
            "name": name.strip(),
            "password_hash": self._hash_password(password),
            "status": "active",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        self.store.create_dashboard_user(user)
        self.store.upsert_organization_member(
            {
                "organization_id": organization_id,
                "user_id": user["id"],
                "principal_id": f"person_dashboard_{user['id']}",
                "role": "owner",
                "status": "active",
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        return self._public_user(user)

    def authenticate(self, email: str, password: str) -> tuple[dict[str, Any], dict[str, Any]]:
        user = self.store.dashboard_user_by_email(normalize_email(email))
        if not user or user["status"] != "active":
            # Keep unknown-account timing close to a real password check so
            # the login endpoint does not become an email enumeration oracle.
            hashlib.scrypt(
                password.encode(), salt=b"toda-login-dummy", n=2**14, r=8, p=1, dklen=32
            )
            raise ValueError("Invalid email or password")
        accepted = self._password_matches(user["password_hash"], password)
        if not accepted:
            raise ValueError("Invalid email or password")
        memberships = [
            member
            for organization in self.store.list_organizations()
            if (
                member := self.store.organization_member(organization["id"], user["id"])
            )
            and member["status"] == "active"
        ]
        if len(memberships) != 1:
            raise PermissionError("Your account is waiting for organization access")
        return self._public_user(user), memberships[0]

    def create_session(self, organization_id: str, user_id: str) -> dict[str, Any]:
        timestamp = utcnow()
        token = secrets.token_urlsafe(32)
        session = {
            "id": hashlib.sha256(token.encode()).hexdigest(),
            "organization_id": organization_id,
            "user_id": user_id,
            "created_at": timestamp.isoformat(),
            "last_seen_at": timestamp.isoformat(),
            "expires_at": (timestamp + timedelta(days=14)).isoformat(),
        }
        self.store.create_dashboard_session(session)
        return {**session, "token": token}

    def resolve_session(self, session_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        session = self.store.dashboard_session(
            hashlib.sha256(session_id.encode()).hexdigest(), utcnow().isoformat()
        )
        if not session:
            return None
        user = self.store.dashboard_user(session["user_id"])
        member = self.store.organization_member(session["organization_id"], session["user_id"])
        if not user or user["status"] != "active" or not member or member["status"] != "active":
            return None
        return self._public_user(user), member

    def revoke_session(self, session_id: str) -> None:
        self.store.revoke_dashboard_session(
            hashlib.sha256(session_id.encode()).hexdigest(), utcnow().isoformat()
        )






