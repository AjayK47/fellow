"""Durable, run-scoped workspace metadata.

The filesystem is still the working surface; this service owns the Docket
record that binds it to a run, principal, scope, quota, and expiry.  Tools must
never accept an arbitrary host path in place of this record.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from docket.domain.models import new_id, utcnow


class WorkspaceService:
    def __init__(self, store: Any, audit: Any, root: Path | str) -> None:
        self.store = store
        self.audit = audit
        self.root = Path(root).expanduser().resolve()

    def create(
        self,
        *,
        organization_id: str,
        run_id: str,
        owner_id: str,
        scope_id: str,
        quota_bytes: int = 10_000_000,
        ttl_seconds: int = 86_400,
    ) -> dict[str, Any]:
        if quota_bytes < 1_024 or quota_bytes > 1_000_000_000:
            raise ValueError("workspace quota must be between 1KB and 1GB")
        if ttl_seconds < 60 or ttl_seconds > 2_592_000:
            raise ValueError("workspace expiry must be between 1 minute and 30 days")
        scope = self.store.get_scope(scope_id)
        if not scope or scope.get("organization_id") != organization_id:
            raise ValueError("workspace scope does not belong to organization")
        now = utcnow()
        workspace_id = new_id("workspace")
        record = {
            "id": workspace_id,
            "organization_id": organization_id,
            "run_id": run_id,
            "owner_id": owner_id,
            "scope_id": scope_id,
            "path": str((self.root / ".docket-workspaces" / run_id).resolve()),
            "quota_bytes": quota_bytes,
            "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
            "status": "active",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        Path(record["path"]).mkdir(parents=True, exist_ok=True)
        self.store.upsert_harness_projection("workspaces", record)
        self.audit.record(
            organization_id,
            "workspace.created",
            {"workspace_id": workspace_id, "run_id": run_id, "scope_id": scope_id},
            principal_id=owner_id,
        )
        return record

    def get_for_run(self, organization_id: str, run_id: str) -> dict[str, Any] | None:
        records = self.store.list_harness_records(
            "workspaces", organization_id, where={"run_id": run_id}, limit=1
        )
        return records[0] if records else None

    def list(self, organization_id: str, *, run_id: str | None = None) -> list[dict[str, Any]]:
        where = {"run_id": run_id} if run_id else None
        return self.store.list_harness_records(
            "workspaces", organization_id, where=where, order_by="updated_at", descending=True
        )

    def expire(self, workspace_id: str, organization_id: str) -> dict[str, Any] | None:
        record = self.store.get_harness_record("workspaces", workspace_id)
        if not record or record.get("organization_id") != organization_id:
            return None
        record["status"] = "expired"
        record["updated_at"] = utcnow().isoformat()
        self.store.upsert_harness_projection("workspaces", record)
        self.audit.record(organization_id, "workspace.expired", {"workspace_id": workspace_id})
        return record
