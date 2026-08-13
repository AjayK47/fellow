"""First-run installation bootstrap.

Creates the single organization this gateway serves, its public scope, and
the owner dashboard account. Driven by ``toda init``; the gateway refuses to
start until it has run.

This logic was previously buried in the setup wizard's HTTP service. It is
load-bearing well beyond onboarding: the literal scope id ``scope_org_public``
is assumed by the seed fixtures, connector ingestion fallbacks, skill
packages, and scope resolution, so nothing else in the codebase can create a
usable tenant.
"""

from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone
from typing import Any

from toda.services.dashboard_auth import DashboardAuthService
from toda.storage.db import BrainStore

# Every other default in the codebase resolves the organization's public
# scope by this exact literal rather than looking it up by kind. An
# org-id-prefixed id here would silently mismatch all of them.
PUBLIC_SCOPE_ID = "scope_org_public"


def generate_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def ensure_organization(store: BrainStore, *, organization_id: str, name: str) -> str:
    """Create the organization and its public scope if absent.

    Idempotent, so re-running ``toda init`` against an initialised database
    is safe. Refuses to add a second organization: one gateway serves one
    tenant, and the retention/authorization paths assume that.
    """
    organizations = store.list_organizations()
    if organizations and all(item["id"] != organization_id for item in organizations):
        raise ValueError(
            f"This installation already belongs to organization "
            f"{organizations[0]['id']!r}; config.json asks for {organization_id!r}"
        )
    if not store.get_organization(organization_id):
        store.put_organization({"id": organization_id, "name": name})

    existing = next(
        (
            scope
            for scope in store.list_scopes(organization_id)
            if scope.get("is_public") and scope.get("kind") == "organization_public"
        ),
        None,
    )
    if existing:
        return str(existing["id"])
    if not store.get_scope(PUBLIC_SCOPE_ID):
        store.upsert_scope(
            {
                "id": PUBLIC_SCOPE_ID,
                "organization_id": organization_id,
                "kind": "organization_public",
                "label": "Organization Public",
                "principal_ids": [],
                "is_public": True,
            }
        )
    return PUBLIC_SCOPE_ID


def register_slack_installation(
    store: BrainStore,
    *,
    organization_id: str,
    workspace_id: str,
    workspace_name: str,
    bot_user_id: str,
    bot_token: str,
) -> dict[str, Any]:
    """Record the workspace this gateway is connected to.

    Without a ``slack_installations`` row, organization resolution for every
    incoming event silently falls back to the configured default instead of
    a real per-workspace mapping. Credentials themselves live in
    config.json; this only persists the mapping.
    """
    now = datetime.now(timezone.utc).isoformat()
    installation = {
        "id": f"slack:{organization_id}:{workspace_id}",
        "organization_id": organization_id,
        "workspace_id": workspace_id,
        "workspace_name": workspace_name or workspace_id,
        "bot_user_id": bot_user_id,
        "bot_token": bot_token,
        "created_at": now,
        "updated_at": now,
    }
    store.upsert_slack_installation(installation)
    return installation


def is_initialized(store: BrainStore, *, organization_id: str) -> bool:
    return bool(store.get_organization(organization_id)) and not DashboardAuthService(
        store
    ).needs_owner


def initialize(
    store: BrainStore,
    *,
    organization_id: str,
    organization_name: str,
    owner_email: str,
    owner_name: str,
    password: str | None = None,
) -> dict[str, Any]:
    """Create the organization, public scope, and owner account.

    Returns the owner record plus the password, which the caller prints
    once. A generated password is never persisted in readable form -- only
    its hash reaches the database.
    """
    scope_id = ensure_organization(store, organization_id=organization_id, name=organization_name)
    auth = DashboardAuthService(store)
    if not auth.needs_owner:
        raise ValueError("This installation already has an owner account")
    secret = password or generate_password()
    owner = auth.create_owner(
        organization_id=organization_id,
        email=owner_email,
        name=owner_name,
        password=secret,
    )
    return {"organization_id": organization_id, "scope_id": scope_id, "owner": owner,
            "password": secret, "generated": password is None}
