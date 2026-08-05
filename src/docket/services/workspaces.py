"""Durable, per-Slack-workspace workspace metadata.

The filesystem is still the working surface; this service owns the Docket
record that binds it to an organization, a Slack workspace (or an
organization-level default when a Task has no Slack destination), a
principal, a scope, and a quota. Tools must never accept an arbitrary host
path in place of this record.

One folder is shared across every channel/conversation in a Slack team --
there is no per-Task folder and no TTL expiry. Retention is an org-level
policy decision (see ``expire()``), not something that happens
automatically to an individual Task's folder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from docket.domain.models import utcnow


class WorkspaceService:
    def __init__(self, store: Any, audit: Any, root: Path | str) -> None:
        self.store = store
        self.audit = audit
        self.root = Path(root).expanduser().resolve()

    @staticmethod
    def _folder_key(organization_id: str, slack_workspace_id: str | None) -> str:
        return slack_workspace_id or f"org-{organization_id}-default"

    def _record_id(self, organization_id: str, slack_workspace_id: str | None) -> str:
        return f"workspace:{organization_id}:{self._folder_key(organization_id, slack_workspace_id)}"

    def get_or_create(
        self,
        *,
        organization_id: str,
        owner_id: str,
        scope_id: str,
        slack_workspace_id: str | None = None,
        quota_bytes: int = 10_000_000,
    ) -> dict[str, Any]:
        if quota_bytes < 1_024 or quota_bytes > 1_000_000_000:
            raise ValueError("workspace quota must be between 1KB and 1GB")
        scope = self.store.get_scope(scope_id)
        if not scope or scope.get("organization_id") != organization_id:
            raise ValueError("workspace scope does not belong to organization")

        record_id = self._record_id(organization_id, slack_workspace_id)
        existing = self.store.get_harness_record("workspaces", record_id)
        if existing and existing.get("organization_id") == organization_id:
            return existing

        now = utcnow()
        folder_key = self._folder_key(organization_id, slack_workspace_id)
        record = {
            "id": record_id,
            "organization_id": organization_id,
            "owner_id": owner_id,
            "scope_id": scope_id,
            "slack_workspace_id": slack_workspace_id,
            "path": str((self.root / ".docket-workspaces" / folder_key).resolve()),
            "quota_bytes": quota_bytes,
            "status": "active",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        Path(record["path"]).mkdir(parents=True, exist_ok=True)
        self.store.upsert_harness_projection("workspaces", record)
        self.audit.record(
            organization_id,
            "workspace.created",
            {
                "workspace_id": record_id,
                "scope_id": scope_id,
                "slack_workspace_id": slack_workspace_id,
            },
            principal_id=owner_id,
        )
        return record

    def get_for_task(
        self, organization_id: str, task: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Resolve the persistent workspace folder for a Task.

        A Task carries its Slack workspace id (if any) at
        ``task["metadata"]["slack"]["workspace_id"]``. Tasks sharing that id
        resolve to the same folder regardless of which channel/conversation
        triggered them; a Task with no Slack destination falls back to the
        organization's default folder.
        """
        slack_workspace_id = ((task or {}).get("metadata") or {}).get("slack", {}).get(
            "workspace_id"
        )
        record = self.store.get_harness_record(
            "workspaces", self._record_id(organization_id, slack_workspace_id)
        )
        if record and record.get("organization_id") == organization_id:
            return record
        return None

    def list(self, organization_id: str) -> list[dict[str, Any]]:
        return self.store.list_harness_records(
            "workspaces", organization_id, order_by="updated_at", descending=True
        )

    def expire(self, workspace_id: str, organization_id: str) -> dict[str, Any] | None:
        """Manually archive a workspace folder's record as an org retention decision."""
        record = self.store.get_harness_record("workspaces", workspace_id)
        if not record or record.get("organization_id") != organization_id:
            return None
        record["status"] = "expired"
        record["updated_at"] = utcnow().isoformat()
        self.store.upsert_harness_projection("workspaces", record)
        self.audit.record(organization_id, "workspace.expired", {"workspace_id": workspace_id})
        return record
