"""Explicit, auditable retention and tenant-deletion controls."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from docket.domain.models import utcnow


_TIMED_TABLES: dict[str, str] = {
    "agent_messages": "created_at",
    "agent_turns": "created_at",
    "harness_tool_calls": "created_at",
    "approvals": "created_at",
    "attention_evaluations": "created_at",
    "agent_interventions": "created_at",
    "agent_feedback": "created_at",
    "deliveries": "created_at",
    "checkpoints": "created_at",
    "episodes": "created_at",
    "received_events": "received_at",
    "processing_jobs": "created_at",
    "harness_jobs": "created_at",
    "connector_objects": "observed_at",
    "connector_sync_runs": "created_at",
    "entity_resolution_candidates": "created_at",
    "fact_conflicts": "created_at",
    "cases": "created_at",
    "case_events": "created_at",
    "judgment_events": "created_at",
    "policy_versions": "created_at",
    "action_preconditions": "created_at",
    "artifacts": "created_at",
}

_TENANT_TABLES = [
    "chunks_fts",
    "memories_fts",
    "memory_embeddings",
    "chunks",
    "principal_memberships",
    "skill_versions",
    "agent_feedback",
    "deliveries",
    "checkpoints",
    "approvals",
    "harness_tool_calls",
    "agent_turns",
    "agent_messages",
    "agent_runs",
    "agent_sessions",
    "attention_evaluations",
    "agent_interventions",
    "conversation_states",
    "harness_jobs",
    "episodes",
    "automations",
    "slack_installations",
    "channel_policies",
    "identity_mappings",
    "connector_accounts",
    "tool_policies",
    "action_preconditions",
    "policy_versions",
    "judgment_events",
    "case_events",
    "cases",
    "fact_conflicts",
    "reality_facts",
    "entity_resolution_candidates",
    "external_identifiers",
    "connector_sync_cursors",
    "connector_sync_runs",
    "connector_objects",
    "artifacts",
    "processing_jobs",
    "received_events",
    "idempotency_keys",
    "profiles",
    "authz_decisions",
    "audit_events",
    "signals",
    "runs",
    "skills",
    "scopes",
    "memories",
    "edges",
    "entities",
    "sources",
]


class RetentionService:
    """Retention is opt-in and previewable; no cleanup happens silently."""

    def __init__(self, *, store: Any, audit: Any) -> None:
        self.store = store
        self.audit = audit

    def preview(
        self, organization_id: str, *, older_than_days: int
    ) -> dict[str, Any]:
        if older_than_days < 1:
            raise ValueError("older_than_days must be at least 1")
        cutoff = (utcnow() - timedelta(days=older_than_days)).isoformat()
        counts: dict[str, int] = {}
        with self.store.cursor() as cur:
            for table, column in _TIMED_TABLES.items():
                cur.execute(
                    f"""SELECT COUNT(*) AS count FROM {table}
                    WHERE organization_id = ? AND {column} < ?""",
                    (organization_id, cutoff),
                )
                counts[table] = int(cur.fetchone()["count"])
        return {
            "organization_id": organization_id,
            "older_than_days": older_than_days,
            "cutoff": cutoff,
            "counts": counts,
            "total": sum(counts.values()),
        }

    def apply(
        self,
        organization_id: str,
        *,
        older_than_days: int,
        confirmed_by: str,
    ) -> dict[str, Any]:
        preview = self.preview(
            organization_id, older_than_days=older_than_days
        )
        deleted: dict[str, int] = {}
        with self.store.cursor() as cur:
            for table, column in _TIMED_TABLES.items():
                cur.execute(
                    f"""DELETE FROM {table}
                    WHERE organization_id = ? AND {column} < ?""",
                    (organization_id, preview["cutoff"]),
                )
                deleted[table] = max(cur.rowcount, 0)
        result = {
            **preview,
            "deleted": deleted,
            "deleted_total": sum(deleted.values()),
            "confirmed_by": confirmed_by,
        }
        self.audit.record(
            organization_id,
            "retention.applied",
            result,
            principal_id=confirmed_by,
        )
        return result

    def delete_organization(
        self, organization_id: str, *, confirmation: str
    ) -> dict[str, Any]:
        if confirmation != organization_id:
            raise ValueError(
                "confirmation must exactly match organization_id"
            )
        counts: dict[str, int] = {}
        with self.store.cursor() as cur:
            for table in _TENANT_TABLES:
                cur.execute(
                    f"DELETE FROM {table} WHERE organization_id = ?",
                    (organization_id,),
                )
                counts[table] = max(cur.rowcount, 0)
            cur.execute(
                "DELETE FROM organizations WHERE id = ?", (organization_id,)
            )
            counts["organizations"] = max(cur.rowcount, 0)
        return {
            "organization_id": organization_id,
            "deleted": counts,
            "deleted_total": sum(counts.values()),
            "recoverable": False,
        }
