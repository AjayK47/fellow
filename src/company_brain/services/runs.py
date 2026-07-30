"""Harness run telemetry → execution graph."""

from __future__ import annotations

from typing import Any

from company_brain.domain.models import RunStatus, TaskRun, new_id, utcnow
from company_brain.services.audit import AuditService
from company_brain.services.graph import GraphService
from company_brain.storage.db import BrainStore


class RunsService:
    def __init__(self, store: BrainStore, graph: GraphService, audit: AuditService) -> None:
        self.store = store
        self.graph = graph
        self.audit = audit

    def start(
        self,
        *,
        organization_id: str,
        task: str,
        acting_principal_id: str,
        harness_id: str,
        skill_id: str | None = None,
        target_entity_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        run = TaskRun(
            id=new_id("run"),
            organization_id=organization_id,
            task=task,
            acting_principal_id=acting_principal_id,
            harness_id=harness_id,
            skill_id=skill_id,
            target_entity_ids=target_entity_ids or [],
            status=RunStatus.STARTED,
            events=[
                {
                    "event_type": "run.started",
                    "at": utcnow().isoformat(),
                    "task": task,
                }
            ],
        )
        data = run.model_dump(mode="json")
        self.store.upsert_run(data)

        # execution graph nodes/edges
        self.graph.upsert_entity(
            {
                "id": data["id"],
                "organization_id": organization_id,
                "entity_type": "task",
                "name": f"Run: {task[:80]}",
                "attributes": {"kind": "task_run", "status": data["status"]},
            }
        )
        self.graph.add_edge(
            organization_id,
            "EXECUTED_BY",
            data["id"],
            acting_principal_id,
        )
        if skill_id:
            self.graph.add_edge(organization_id, "USES_SKILL", data["id"], skill_id)
        for eid in target_entity_ids or []:
            self.graph.add_edge(organization_id, "TARGETS", data["id"], eid)

        self.audit.record(
            organization_id=organization_id,
            event_type="run.started",
            principal_id=acting_principal_id,
            payload={"run_id": data["id"], "task": task, "harness_id": harness_id},
        )
        return data

    def add_event(self, run_id: str, event: dict[str, Any]) -> dict[str, Any] | None:
        run = self.store.get_run(run_id)
        if not run:
            return None
        event = dict(event)
        event.setdefault("at", utcnow().isoformat())
        events = list(run.get("events") or [])
        events.append(event)
        run["events"] = events
        run["status"] = RunStatus.RUNNING.value

        # execution graph action node
        action_id = new_id("action")
        self.graph.upsert_entity(
            {
                "id": action_id,
                "organization_id": run["organization_id"],
                "entity_type": "task",
                "name": event.get("action") or event.get("event_type") or "action",
                "attributes": {"kind": "action", "event": event},
            }
        )
        self.graph.add_edge(run["organization_id"], "HAS_ACTION", run_id, action_id)
        if event.get("target_entity_id"):
            self.graph.add_edge(
                run["organization_id"], "ACTION_TARGET", action_id, event["target_entity_id"]
            )
        for sid in event.get("evidence_source_ids") or []:
            self.graph.add_edge(run["organization_id"], "SUPPORTED_BY", action_id, sid)

        self.store.upsert_run(run)
        self.audit.record(
            organization_id=run["organization_id"],
            event_type="run.event",
            payload={"run_id": run_id, "event": event, "action_id": action_id},
        )
        return run

    def complete(
        self,
        run_id: str,
        *,
        outcome: str,
        summary: str | None = None,
        evidence_source_ids: list[str] | None = None,
    ) -> dict[str, Any] | None:
        run = self.store.get_run(run_id)
        if not run:
            return None
        run["status"] = RunStatus.COMPLETED.value if outcome != "failed" else RunStatus.FAILED.value
        run["outcome"] = outcome
        run["summary"] = summary
        run["completed_at"] = utcnow().isoformat()
        events = list(run.get("events") or [])
        events.append(
            {
                "event_type": "run.completed",
                "at": run["completed_at"],
                "outcome": outcome,
                "summary": summary,
                "evidence_source_ids": evidence_source_ids or [],
            }
        )
        run["events"] = events
        self.store.upsert_run(run)

        # outcome edge
        outcome_id = new_id("outcome")
        self.graph.upsert_entity(
            {
                "id": outcome_id,
                "organization_id": run["organization_id"],
                "entity_type": "task",
                "name": f"Outcome: {outcome}",
                "attributes": {"kind": "outcome", "summary": summary},
            }
        )
        self.graph.add_edge(run["organization_id"], "RESULTED_IN", run_id, outcome_id)

        self.audit.record(
            organization_id=run["organization_id"],
            event_type="run.completed",
            payload={"run_id": run_id, "outcome": outcome, "summary": summary},
        )
        return run

    def get(self, run_id: str) -> dict[str, Any] | None:
        return self.store.get_run(run_id)

    def list(self, organization_id: str) -> list[dict[str, Any]]:
        return self.store.list_runs(organization_id)

    def execution_graph_view(self, run_id: str) -> dict[str, Any] | None:
        run = self.get(run_id)
        if not run:
            return None
        org = run["organization_id"]
        edges = self.graph.neighbors(org, run_id)
        related_ids = {run_id}
        for e in edges:
            related_ids.add(e["from_id"])
            related_ids.add(e["to_id"])
        nodes = []
        for nid in related_ids:
            ent = self.store.get_entity(nid)
            if ent:
                nodes.append(ent)
            elif nid == run_id:
                nodes.append({"id": run_id, "entity_type": "task", "name": run.get("task")})
        return {"run": run, "nodes": nodes, "edges": edges}

    def evaluate(self, run_id: str) -> dict[str, Any] | None:
        """Return deterministic run-learning signals; approval remains a human skill action."""
        run = self.get(run_id)
        if not run:
            return None
        events = run.get("events") or []
        actions = [event for event in events if event.get("event_type") not in {"run.started", "run.completed"}]
        outcome = run.get("outcome") or "unknown"
        result = {
            "run_id": run_id,
            "outcome": outcome,
            "status": run.get("status"),
            "action_count": len(actions),
            "successful_actions": sum(1 for action in actions if str(action.get("result", "")).lower() in {"success", "succeeded", "completed"}),
            "evidence_source_ids": sorted({sid for event in events for sid in event.get("evidence_source_ids") or []}),
            "skill_update_candidate": {
                "skill_id": run.get("skill_id"),
                "recommended": bool(run.get("skill_id") and outcome not in {"resolved", "success", "completed"}),
                "reason": "Review failed or incomplete runs before proposing a new skill version." if run.get("skill_id") and outcome not in {"resolved", "success", "completed"} else "No automatic skill update proposed.",
            },
        }
        self.audit.record(run["organization_id"], "run.evaluated", result)
        return result
