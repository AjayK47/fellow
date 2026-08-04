"""Bounded, ephemeral specialist reasoning workers owned by the main agent."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from typing import Any

from docket.domain.models import new_id, utcnow
from docket.harness.models import (
    AgentRunState,
    DestinationEnvelope,
    InvocationEnvelope,
    ToolDefinition,
    TriggerKind,
)
from docket.services.scope_resolution import resolve_scope


class SpecialistWorkerService:
    def __init__(
        self,
        *,
        agent: Any,
        artifacts: Any,
        audit: Any,
        max_parallel: int = 3,
        max_depth: int = 1,
    ) -> None:
        self.agent = agent
        self.artifacts = artifacts
        self.audit = audit
        self.max_parallel = max(1, max_parallel)
        self.max_depth = max(1, max_depth)

    @staticmethod
    def tool_definition() -> ToolDefinition:
        return ToolDefinition(
            name="delegate_work",
            description=(
                "Create one to three bounded specialist workers for independent "
                "investigation, analysis, or action. A worker can be granted any "
                "tool named in allowed_tools, including write tools — write-risk "
                "tools still pause for the same human approval they'd need from "
                "the main agent. Use only when separate context windows or "
                "parallel work materially help. Each worker must have a distinct "
                "objective and structured return contract."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 3,
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {"type": "string"},
                                "objective": {"type": "string"},
                                "allowed_tools": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "output_contract": {"type": "string"},
                                "max_iterations": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 10,
                                },
                                "max_tool_calls": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 16,
                                },
                                "token_budget": {
                                    "type": "integer",
                                    "minimum": 1000,
                                    "maximum": 40000,
                                },
                                "deadline_seconds": {
                                    "type": "integer",
                                    "minimum": 10,
                                    "maximum": 900,
                                },
                                "allowed_scope_ids": {
                                    "type": "array", "items": {"type": "string"}
                                },
                                "capabilities": {
                                    "type": "array", "items": {"type": "string"}
                                },
                                "dependency_ids": {
                                    "type": "array", "items": {"type": "string"}
                                },
                                "review_required": {"type": "boolean"},
                            },
                            "required": [
                                "role",
                                "objective",
                                "allowed_tools",
                                "output_contract",
                            ],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["tasks"],
                "additionalProperties": False,
            },
            risk="read",
        )

    def execute(
        self, arguments: dict[str, Any], runtime: dict[str, Any]
    ) -> dict[str, Any]:
        parent = self.agent.runs.get(runtime["run_id"])
        if not parent:
            raise KeyError(runtime["run_id"])
        depth = int((parent.get("metadata") or {}).get("worker_depth", 0))
        if depth >= self.max_depth:
            raise PermissionError("specialist workers cannot delegate further")
        tasks = list(arguments.get("tasks") or [])
        if not tasks or len(tasks) > self.max_parallel:
            raise ValueError(
                f"between one and {self.max_parallel} specialist tasks are required"
            )
        seen: set[str] = set()
        for task in tasks:
            objective = " ".join(task["objective"].lower().split())
            if objective in seen:
                raise ValueError("specialist objectives must be distinct")
            seen.add(objective)
        results: list[dict[str, Any]] = []
        for task in tasks:
            task_id = new_id("specialist_task")
            task["_task_id"] = task_id
            now = utcnow().isoformat()
            self.agent.runs.store.upsert_harness_projection(
                "specialist_tasks",
                {
                    "id": task_id,
                    "organization_id": parent["organization_id"],
                    "run_id": parent["id"],
                    "status": "queued",
                    "role": task["role"],
                    "objective": task["objective"],
                    "allowed_tools": sorted(set(task.get("allowed_tools") or [])),
                    "allowed_scope_ids": task.get("allowed_scope_ids") or parent["envelope"].get("allowed_scope_ids") or [],
                    "capabilities": task.get("capabilities") or [],
                    "dependency_ids": task.get("dependency_ids") or [],
                    "review_required": bool(task.get("review_required", False)),
                    "created_at": now,
                    "updated_at": now,
                },
            )
        with ThreadPoolExecutor(
            max_workers=min(len(tasks), self.max_parallel),
            thread_name_prefix="company-specialist",
        ) as pool:
            futures = [
                pool.submit(self._run_one, parent, task, depth + 1)
                for task in tasks
            ]
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda item: item["role"])
        return {"workers": results, "count": len(results)}

    def _run_one(
        self, parent: dict[str, Any], task: dict[str, Any], depth: int
    ) -> dict[str, Any]:
        envelope_data = parent["envelope"]
        destination = DestinationEnvelope(**envelope_data["destination"])
        requested_scopes = set(task.get("allowed_scope_ids") or [])
        inherited_scopes = set(envelope_data.get("allowed_scope_ids") or [])
        worker_scope_ids = sorted(requested_scopes & inherited_scopes) if requested_scopes else sorted(inherited_scopes)
        task_id = task.get("_task_id")
        if task_id:
            record = self.agent.runs.store.get_harness_record("specialist_tasks", task_id) or {}
            record.update({"status": "running", "updated_at": utcnow().isoformat()})
            self.agent.runs.store.upsert_harness_projection("specialist_tasks", record)
        envelope = InvocationEnvelope(
            organization_id=parent["organization_id"],
            principal_id=envelope_data["principal_id"],
            application_id=envelope_data.get("application_id", "docket"),
            conversation_id=(
                f"worker:{parent['id']}:{task['role']}:"
                f"{abs(hash(task['objective']))}"
            ),
            destination=destination,
            purpose="specialist_worker",
            permission_decision_id=envelope_data["permission_decision_id"],
            allowed_scope_ids=worker_scope_ids,
            delegated_by=parent["id"],
            trace_id=parent["trace_id"],
        )
        allowed = sorted(
            set(task.get("allowed_tools") or [])
            - {"delegate_work", "request_human_input"}
        )
        has_write_tools = any(
            registered and registered.definition.risk != "read"
            for registered in (self.agent.tools.registry.get(name) for name in allowed)
        )
        worker_goal = (
            f"Role: {task['role']}\n"
            f"Objective: {task['objective']}\n"
            f"Return contract: {task['output_contract']}\n"
            "Return only findings supported by tool evidence. Include source IDs, "
            "uncertainties, and recommended next steps. Do not address the user."
            + (
                " Any write tool you were granted still pauses for the same "
                "human approval it would need from the main agent — you are not "
                "exempt from that gate."
                if has_write_tools
                else " Do not perform external writes."
            )
        )
        run = self.agent.create_invocation(
            envelope=envelope,
            goal=worker_goal,
            trigger_kind=TriggerKind.TOOL_CONTINUATION,
            initial_message=worker_goal,
            parent_run_id=parent["id"],
            metadata={
                "specialist_worker": True,
                "worker_role": task["role"],
                "worker_depth": depth,
                "allowed_tools": allowed,
                "worker_has_write_tools": has_write_tools,
                "output_contract": task["output_contract"],
                "suppress_delivery": True,
                "worker_capabilities": task.get("capabilities") or [],
                "review_required": bool(task.get("review_required", False)),
            },
        )
        run["max_iterations"] = min(int(task.get("max_iterations", 6)), 10)
        run["max_tool_calls"] = min(int(task.get("max_tool_calls", 10)), 16)
        run["max_total_tokens"] = min(
            int(task.get("token_budget", 20_000)), 40_000
        )
        run["deadline_at"] = (
            utcnow()
            + timedelta(
                seconds=min(int(task.get("deadline_seconds", 180)), 900)
            )
        ).isoformat()
        self.agent.runs.update(run)
        if task_id:
            record = self.agent.runs.store.get_harness_record("specialist_tasks", task_id) or {}
            record.update({"worker_run_id": run["id"], "updated_at": utcnow().isoformat()})
            self.agent.runs.store.upsert_harness_projection("specialist_tasks", record)
        outcome = self.agent.run(run["id"])
        if outcome.state == AgentRunState.WAITING_FOR_APPROVAL:
            # Nothing resumes this automatically today — a human has to act
            # on the approval, and the parent has no way to be woken back up
            # when they do. Say so plainly instead of returning an empty
            # "finding" that looks like the worker had nothing to report.
            answer = (
                f"Paused: a write action from this worker is waiting on human "
                f"approval (approval_id={outcome.approval_id}). It will not "
                "resume on its own — decide the approval, then re-run this "
                "task if the result is still needed."
            )
        else:
            answer = outcome.answer or outcome.error or ""
        scope_id = resolve_scope(
            store=self.agent.runs.store,
            organization_id=parent["organization_id"],
            principal_id=envelope.principal_id,
            destination=envelope_data["destination"],
        ).write_scope_id
        artifact = self.artifacts.create(
            organization_id=parent["organization_id"],
            artifact_type="specialist_result",
            title=f"{task['role']}: {task['objective'][:100]}",
            content={
                "role": task["role"],
                "objective": task["objective"],
                "output_contract": task["output_contract"],
                "answer": answer,
                "state": outcome.state.value,
            },
            scope_id=scope_id,
            run_id=run["id"],
            created_by=envelope.principal_id,
            metadata={"parent_run_id": parent["id"]},
        )
        self.audit.record(
            parent["organization_id"],
            "agent.specialist.completed",
            {
                "parent_run_id": parent["id"],
                "worker_run_id": run["id"],
                "role": task["role"],
                "artifact_id": artifact["id"],
                "state": outcome.state.value,
            },
            principal_id=envelope.principal_id,
            trace_id=parent["trace_id"],
        )
        if task_id:
            record = self.agent.runs.store.get_harness_record("specialist_tasks", task_id) or {}
            record.update(
                {
                    "status": outcome.state.value,
                    "artifact_id": artifact["id"],
                    "updated_at": utcnow().isoformat(),
                }
            )
            self.agent.runs.store.upsert_harness_projection("specialist_tasks", record)
            now = utcnow().isoformat()
            self.agent.runs.store.upsert_harness_projection(
                "handoffs",
                {
                    "id": new_id("handoff"),
                    "organization_id": parent["organization_id"],
                    "run_id": parent["id"],
                    "status": (
                        "awaiting_review"
                        if task.get("review_required")
                        or outcome.state == AgentRunState.WAITING_FOR_APPROVAL
                        else "accepted"
                    ),
                    "specialist_task_id": task_id,
                    "worker_run_id": run["id"],
                    "artifact_id": artifact["id"],
                    "summary": answer[:4000],
                    "created_at": now,
                    "updated_at": now,
                },
            )
        return {
            "role": task["role"],
            "worker_run_id": run["id"],
            "state": outcome.state.value,
            "artifact_id": artifact["id"],
            "summary": answer[:4000],
        }
