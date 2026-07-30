"""Durable interval automations that activate the same CompanyAgent runtime."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from company_brain.domain.models import new_id, utcnow
from company_brain.harness.models import InvocationEnvelope, TriggerKind


class AutomationService:
    def __init__(
        self,
        *,
        store: Any,
        authz: Any,
        agent: Any,
        queue: Any,
        audit: Any,
    ) -> None:
        self.store = store
        self.authz = authz
        self.agent = agent
        self.queue = queue
        self.audit = audit

    def create(
        self,
        *,
        organization_id: str,
        name: str,
        goal: str,
        principal_id: str,
        conversation_id: str,
        destination: dict[str, Any],
        interval_seconds: int,
        enabled: bool = True,
    ) -> dict[str, Any]:
        if interval_seconds < 60:
            raise ValueError("automation interval must be at least 60 seconds")
        now = utcnow()
        automation = {
            "id": new_id("automation"),
            "organization_id": organization_id,
            "name": name,
            "goal": goal,
            "principal_id": principal_id,
            "conversation_id": conversation_id,
            "destination": destination,
            "interval_seconds": interval_seconds,
            "status": "enabled" if enabled else "disabled",
            "next_run_at": (now + timedelta(seconds=interval_seconds)).isoformat()
            if enabled
            else None,
            "last_run_id": None,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        self._upsert(automation)
        return automation

    def list(self, organization_id: str) -> list[dict[str, Any]]:
        return self.store.list_harness_records(
            "automations", organization_id, order_by="updated_at"
        )

    def tick(self, organization_id: str | None = None) -> dict[str, Any]:
        now = utcnow().isoformat()
        with self.store.cursor() as cur:
            if organization_id:
                cur.execute(
                    """SELECT payload FROM automations
                    WHERE organization_id = ? AND status = 'enabled'
                    AND next_run_at <= ? ORDER BY next_run_at""",
                    (organization_id, now),
                )
            else:
                cur.execute(
                    """SELECT payload FROM automations
                    WHERE status = 'enabled' AND next_run_at <= ?
                    ORDER BY next_run_at""",
                    (now,),
                )
            due = [__import__("json").loads(row["payload"]) for row in cur.fetchall()]
        runs: list[dict[str, Any]] = []
        for automation in due:
            decision = self.authz.resolve(
                organization_id=automation["organization_id"],
                acting_principal_id=automation["principal_id"],
                destination=automation["destination"],
                purpose="scheduled_automation",
            )
            envelope = InvocationEnvelope(
                organization_id=automation["organization_id"],
                principal_id=automation["principal_id"],
                application_id="company_brain_scheduler",
                conversation_id=automation["conversation_id"],
                destination=automation["destination"],
                purpose="scheduled_automation",
                permission_decision_id=decision.id,
                allowed_scope_ids=decision.allowed_scope_ids,
            )
            run = self.agent.create_invocation(
                envelope=envelope,
                goal=automation["goal"],
                trigger_kind=TriggerKind.AUTOMATION,
                initial_message=(
                    f"Scheduled automation '{automation['name']}': "
                    f"{automation['goal']}"
                ),
                metadata={"automation_id": automation["id"]},
            )
            self.queue.enqueue(
                organization_id=automation["organization_id"],
                kind="agent.run",
                payload={"run_id": run["id"]},
                idempotency_key=f"automation.run:{automation['id']}:{automation['next_run_at']}",
            )
            automation["last_run_id"] = run["id"]
            automation["next_run_at"] = (
                utcnow()
                + timedelta(seconds=int(automation["interval_seconds"]))
            ).isoformat()
            automation["updated_at"] = utcnow().isoformat()
            self._upsert(automation)
            runs.append(run)
            self.audit.record(
                automation["organization_id"],
                "automation.fired",
                {"automation_id": automation["id"], "run_id": run["id"]},
                principal_id=automation["principal_id"],
                trace_id=run["trace_id"],
            )
        return {"fired": len(runs), "run_ids": [run["id"] for run in runs]}

    def set_enabled(self, automation_id: str, enabled: bool) -> dict[str, Any]:
        automation = self.store.get_harness_record("automations", automation_id)
        if not automation:
            raise KeyError(automation_id)
        automation["status"] = "enabled" if enabled else "disabled"
        automation["next_run_at"] = (
            (
                utcnow()
                + timedelta(seconds=int(automation["interval_seconds"]))
            ).isoformat()
            if enabled
            else None
        )
        automation["updated_at"] = utcnow().isoformat()
        self._upsert(automation)
        return automation

    def _upsert(self, automation: dict[str, Any]) -> None:
        with self.store.cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO automations
                (id, organization_id, status, next_run_at, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    automation["id"],
                    automation["organization_id"],
                    automation["status"],
                    automation.get("next_run_at"),
                    __import__("json").dumps(automation),
                    automation["created_at"],
                    automation["updated_at"],
                ),
            )
