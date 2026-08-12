"""ChildTaskService: durable child-worker delegation for the Task tree.

Where synchronous delegation would block waiting for every child to finish
before returning, delegate_tasks creates child Tasks, enqueues each one for
independent execution by a worker (ChildTaskExecutor, dispatched off the
durable job queue), and immediately pauses the calling Task at
WAITING_FOR_CHILDREN -- releasing its execution lease so no Python thread
blocks on the children. Each child's terminal transition notifies the owning
Session, which is what eventually lets the Session Manager scheduler resume
this Task via CompanyAgent.resume_for_children().

Delegation schema is deliberately minimal: worker type, the complete Task
prompt, and optional prompt-only specialization.  Specialization never creates
a persistent agent identity or lifecycle; it only focuses the temporary worker
that owns this child Task attempt.
This is the only delegation surface. Every invocation is attached to the
current Task and creates durable child Tasks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from toda.domain.models import new_id, utcnow
from toda.harness.models import AgentRunState, ToolDefinition, TriggerKind

_SUPPORTED_WORKER_TYPES = {"general", "pi"}


class AwaitingChildren(Exception):
    """Raised by ChildTaskService.execute() once child Tasks have been
    created and enqueued. Caught by ToolExecutor.execute() and turned into
    a ToolResult(status="waiting_for_children") so _loop() pauses the
    calling Task at WAITING_FOR_CHILDREN instead of blocking for a result."""

    def __init__(self, *, child_task_ids: list[str]) -> None:
        self.child_task_ids = child_task_ids
        super().__init__(f"{len(child_task_ids)} child task(s) dispatched")


_TERMINAL_TASK_STATES = {
    AgentRunState.COMPLETED.value,
    AgentRunState.FAILED.value,
    AgentRunState.CANCELLED.value,
}


class ChildTaskService:
    def __init__(
        self,
        *,
        store: Any,
        queue: Any,
        audit: Any,
        artifacts: Any | None = None,
        workspaces: Any | None = None,
        pi_executor: Any = None,
        max_parallel: int = 3,
        max_depth: int = 2,
        max_iterations: int = 200,
        max_tool_calls: int = 500,
    ) -> None:
        self.store = store
        self.queue = queue
        self.audit = audit
        self.artifacts = artifacts
        self.workspaces = workspaces
        # Set post-construction in container.py -- PiTaskExecutor needs
        # `sessions`, which is built before ChildTaskService's own pi
        # dependency is available.
        self.pi_executor = pi_executor
        self.max_parallel = max(1, max_parallel)
        self.max_depth = max(1, max_depth)
        self.max_iterations = max(1, max_iterations)
        self.max_tool_calls = max(1, max_tool_calls)

    @staticmethod
    def tool_definition() -> ToolDefinition:
        return ToolDefinition(
            name="delegate_tasks",
            description=(
                "Create one to three child Tasks for substantial or specialized "
                "work: worker_type='general' for investigation/analysis/action, "
                "or worker_type='pi' for coding work. Give each child a "
                "complete, plain-language prompt -- no role, objective, or "
                "output contract schema needed. For dependent work, delegate "
                "one prerequisite child, then on its result call plan_get and "
                "delegate the next step. Put two or three children in one call "
                "only when their work is independent and should run concurrently. "
                "You are woken when a child "
                "finishes, fails, or otherwise needs attention, and you "
                "decide whether to wait for the rest, revise or retry a "
                "child, create additional children, synthesize the "
                "available results, ask the user, or finish."
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
                                "worker_type": {
                                    "type": "string",
                                    "enum": sorted(_SUPPORTED_WORKER_TYPES),
                                },
                                "prompt": {"type": "string"},
                                "plan_step_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "maxItems": 20,
                                    "description": (
                                        "Optional root-plan step IDs this child advances. "
                                        "Trusted linkage metadata, not an output contract."
                                    ),
                                },
                                "attachments": {
                                    "type": "array",
                                    "maxItems": 20,
                                    "description": (
                                        "Permission-checked references to durable material; "
                                        "never paste large content into the prompt."
                                    ),
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "kind": {
                                                "type": "string",
                                                "enum": ["artifact", "workspace_file", "source"],
                                            },
                                            "ref": {"type": "string"},
                                            "label": {"type": "string"},
                                        },
                                        "required": ["kind", "ref"],
                                        "additionalProperties": False,
                                    },
                                },
                                "specialist": {
                                    "type": "object",
                                    "description": (
                                        "Optional named specialist identity and focused "
                                        "operating guidance for this child. This changes "
                                        "the prompt, not the Task lifecycle."
                                    ),
                                    "properties": {
                                        "name": {"type": "string"},
                                        "instructions": {"type": "string"},
                                    },
                                    "required": ["name", "instructions"],
                                    "additionalProperties": False,
                                },
                            },
                            "required": ["worker_type", "prompt"],
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
        parent = self.store.get_task(runtime["task_id"])
        if not parent:
            raise KeyError(runtime["task_id"])
        depth = int((parent.get("metadata") or {}).get("worker_depth", 0))
        if depth >= self.max_depth:
            raise PermissionError("child tasks cannot delegate further")
        tasks = list(arguments.get("tasks") or [])
        if not tasks or len(tasks) > self.max_parallel:
            raise ValueError(
                f"between one and {self.max_parallel} child tasks are required"
            )
        for spec in tasks:
            if spec["worker_type"] not in _SUPPORTED_WORKER_TYPES:
                raise ValueError(f"unsupported worker_type: {spec['worker_type']!r}")
        root_task_id = parent.get("root_task_id") or parent["id"]
        existing_child_ids = list(parent.get("pending_child_task_ids") or [])
        unfinished_existing_ids = [
            child_id
            for child_id in existing_child_ids
            if (self.store.get_task(child_id) or {}).get("state")
            not in _TERMINAL_TASK_STATES
        ]
        now = utcnow().isoformat()
        base_child_metadata: dict[str, Any] = {
            "worker_depth": depth + 1,
            "suppress_delivery": True,
            "suppress_memory_capture": True,
        }
        parent_slack = (parent.get("metadata") or {}).get("slack")
        if parent_slack:
            # Needed for WorkspaceService.get_for_task() to resolve the same
            # persistent workspace as the rest of this Task tree -- most
            # relevant for worker_type=pi children.
            base_child_metadata["slack"] = parent_slack
        # Validate the entire batch before persisting any child. Otherwise a
        # bad reference in item two could leave item one as an unqueued orphan.
        prepared: list[tuple[dict[str, Any], list[str], list[dict[str, str]]]] = []
        for spec in tasks:
            prepared.append(
                (
                    spec,
                    self._validate_plan_step_ids(
                        parent, list(spec.get("plan_step_ids") or [])
                    ),
                    self._validate_attachments(
                        parent, list(spec.get("attachments") or [])
                    ),
                )
            )
        created: list[dict[str, Any]] = []
        for spec, plan_step_ids, attachments in prepared:
            specialist = spec.get("specialist") or {}
            specialist_name = specialist.get("name") or (
                "Coding Specialist" if spec["worker_type"] == "pi" else "General Specialist"
            )
            child = {
                "id": new_id("task"),
                "organization_id": parent["organization_id"],
                "session_id": parent["session_id"],
                "parent_task_id": parent["id"],
                "root_task_id": root_task_id,
                "state": AgentRunState.QUEUED.value,
                "worker_type": spec["worker_type"],
                "trigger_kind": TriggerKind.TOOL_CONTINUATION.value,
                "trigger_event_id": None,
                "prompt": spec["prompt"],
                "goal": spec["prompt"],
                "envelope": parent["envelope"],
                "trace_id": parent.get("trace_id"),
                # General specialists may own a long sequential workflow, so
                # their configurable envelope is intentionally much larger
                # than the Session Manager's coordination envelope. Pi Tasks
                # use heartbeat/checkpoint lifecycle controls instead.
                "max_iterations": self.max_iterations,
                "max_tool_calls": self.max_tool_calls,
                "iteration_count": 0,
                "tool_call_count": 0,
                "final_answer": None,
                "result": None,
                "error": None,
                "events": [
                    {
                        "type": "task.created",
                        "at": now,
                        "delegated_by": parent["id"],
                    }
                ],
                "metadata": {
                    **base_child_metadata,
                    "worker_type": spec["worker_type"],
                    "plan_step_ids": plan_step_ids,
                    "attachments": attachments,
                    "specialist_name": specialist_name,
                    "specialist_instructions": specialist.get("instructions") or "",
                    # General specialists can use the entire authorized catalog;
                    # normal principal, destination, policy, risk, and approval
                    # checks still apply to every actual call.
                    "worker_has_write_tools": spec["worker_type"] == "general",
                },
                "created_at": now,
                "updated_at": now,
            }
            self.store.create_task(child)
            # The handoff is also the first durable, Task-local user turn.
            # Child workers load only messages carrying their own task_id by
            # default, while session_search remains available when they need
            # to inspect the wider authorized Session deliberately.
            self.store.append_session_message(
                {
                    "id": new_id("smsg"),
                    "organization_id": child["organization_id"],
                    "session_id": child["session_id"],
                    "task_id": child["id"],
                    "role": "user",
                    "content": child["prompt"],
                    "metadata": {
                        "handoff_from_task_id": parent["id"],
                        "specialist_name": specialist_name,
                        "plan_step_ids": plan_step_ids,
                        "attachment_refs": [item["ref"] for item in attachments],
                    },
                    "created_at": now,
                }
            )
            created.append(child)
        for child in created:
            self.queue.enqueue(
                organization_id=parent["organization_id"],
                kind="task.run_child",
                payload={"task_id": child["id"]},
                idempotency_key=f"task.run_child:{child['id']}",
            )
        self.audit.record(
            parent["organization_id"],
            "task.children.delegated",
            {
                "parent_task_id": parent["id"],
                "root_task_id": root_task_id,
                "child_task_ids": [child["id"] for child in created],
            },
            principal_id=parent["envelope"]["principal_id"],
            trace_id=parent.get("trace_id"),
        )
        # A Manager may add more parallel work after a partial child result.
        # Preserve the still-running prior set instead of orphaning it when
        # runtime.py persists the new waiting state.
        raise AwaitingChildren(
            child_task_ids=[
                *unfinished_existing_ids,
                *[child["id"] for child in created],
            ]
        )

    def _validate_plan_step_ids(
        self, parent: dict[str, Any], requested: list[Any]
    ) -> list[str]:
        step_ids = list(dict.fromkeys(str(item) for item in requested if str(item)))
        if not step_ids:
            return []
        root_task_id = parent.get("root_task_id") or parent["id"]
        plan = self.store.get_harness_record("plans", f"plan:{root_task_id}")
        known = {str(item.get("id")) for item in (plan or {}).get("items") or []}
        unknown = set(step_ids) - known
        if unknown:
            raise ValueError(f"unknown root plan step ids: {sorted(unknown)}")
        return step_ids

    def _validate_attachments(
        self, parent: dict[str, Any], requested: list[Any]
    ) -> list[dict[str, str]]:
        if len(requested) > 20:
            raise ValueError("at most 20 attachment references are allowed")
        organization_id = parent["organization_id"]
        allowed_scopes = set((parent.get("envelope") or {}).get("allowed_scope_ids") or [])
        validated: list[dict[str, str]] = []
        try:
            for raw in requested:
                kind = str(raw.get("kind") or "")
                ref = str(raw.get("ref") or "").strip()
                label = str(raw.get("label") or ref)
                if kind == "artifact":
                    value = self.artifacts.get(ref) if self.artifacts else None
                    if (
                        not value
                        or value.get("organization_id") != organization_id
                        or value.get("status") != "active"
                        or value.get("scope_id") not in allowed_scopes
                    ):
                        raise PermissionError("artifact attachment is not accessible")
                elif kind == "source":
                    value = self.store.get_source(ref)
                    if (
                        not value
                        or value.get("organization_id") != organization_id
                        or value.get("revoked")
                        or value.get("deleted_at")
                        or value.get("scope_id") not in allowed_scopes
                    ):
                        raise PermissionError("source attachment is not accessible")
                elif kind == "workspace_file":
                    if not self.workspaces:
                        raise PermissionError("workspace attachments are unavailable")
                    workspace = self.workspaces.get_for_task(organization_id, parent)
                    if not workspace or workspace.get("status") != "active":
                        raise PermissionError("task workspace is unavailable")
                    scope_id = workspace.get("scope_id")
                    scope = self.store.get_scope(scope_id) if scope_id else None
                    if scope_id not in allowed_scopes and not bool((scope or {}).get("is_public")):
                        raise PermissionError("workspace attachment is outside allowed scopes")
                    root = Path(workspace["path"]).resolve()
                    if Path(ref).is_absolute():
                        raise ValueError("workspace attachment must be a relative path")
                    candidate = (root / ref).resolve()
                    if candidate == root or root not in candidate.parents:
                        raise ValueError("workspace attachment escapes the workspace root")
                    if not candidate.is_file():
                        raise FileNotFoundError("workspace attachment does not exist")
                    ref = str(candidate.relative_to(root))
                else:
                    raise ValueError(f"unsupported attachment kind: {kind!r}")
                validated.append({"kind": kind, "ref": ref, "label": label})
        except Exception as exc:
            self.audit.record(
                organization_id,
                "task.attachment_handoff.rejected",
                {"parent_task_id": parent["id"], "reason": str(exc)},
                principal_id=(parent.get("envelope") or {}).get("principal_id"),
                trace_id=parent.get("trace_id"),
            )
            raise
        return validated

    @staticmethod
    def tool_definition_wait() -> ToolDefinition:
        return ToolDefinition(
            name="wait_for_children",
            description=(
                "Pause this parent Task again while its existing unfinished "
                "children continue. Use after a child checkpoint or partial "
                "completion when the available results are not yet sufficient."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            risk="read",
        )

    def wait(
        self, arguments: dict[str, Any], runtime: dict[str, Any]
    ) -> dict[str, Any]:
        parent = self.store.get_task(runtime["task_id"])
        if not parent:
            raise KeyError(runtime["task_id"])
        child_ids = list(parent.get("pending_child_task_ids") or [])
        unfinished = [
            child_id
            for child_id in child_ids
            if (self.store.get_task(child_id) or {}).get("state")
            not in _TERMINAL_TASK_STATES
        ]
        if not unfinished:
            return {"waiting": False, "reason": "all children are terminal"}
        raise AwaitingChildren(child_task_ids=child_ids)

    @staticmethod
    def tool_definition_start_root() -> ToolDefinition:
        return ToolDefinition(
            name="start_parallel_task",
            description=(
                "Create a separate root Task in this same Session when a new "
                "user request is genuinely unrelated to the root Task you are "
                "currently managing. Do not use this for corrections, steering, "
                "or additions to the current Task. The new root is scheduled "
                "durably and may progress while the current root's children run."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["prompt", "reason"],
                "additionalProperties": False,
            },
            risk="read",
        )

    def start_root(
        self, arguments: dict[str, Any], runtime: dict[str, Any]
    ) -> dict[str, Any]:
        current = self.store.get_task(runtime["task_id"])
        if not current:
            raise KeyError(runtime["task_id"])
        if current.get("parent_task_id") or current.get("worker_type") != "session_manager":
            raise PermissionError("only the Session Manager may create a root Task")
        now = utcnow().isoformat()
        task_id = new_id("task")
        task = {
            "id": task_id,
            "organization_id": current["organization_id"],
            "session_id": current["session_id"],
            "parent_task_id": None,
            "root_task_id": task_id,
            "state": AgentRunState.QUEUED.value,
            "worker_type": "session_manager",
            "trigger_kind": TriggerKind.TOOL_CONTINUATION.value,
            "trigger_event_id": None,
            "prompt": arguments["prompt"],
            "goal": arguments["prompt"],
            "envelope": current["envelope"],
            "trace_id": current.get("trace_id"),
            "max_iterations": current.get("max_iterations", 100),
            "max_tool_calls": current.get("max_tool_calls", 150),
            "iteration_count": 0,
            "tool_call_count": 0,
            "final_answer": None,
            "result": None,
            "error": None,
            "events": [
                {
                    "type": "task.created",
                    "at": now,
                    "reason": arguments["reason"],
                    "created_by_task_id": current["id"],
                }
            ],
            "metadata": {
                **(current.get("metadata") or {}),
                "worker_type": "session_manager",
                "parallel_root_created_by": current["id"],
            },
            "created_at": now,
            "updated_at": now,
        }
        self.store.create_task(task)
        revision = self.store.bump_session_revision(current["session_id"])
        self.queue.enqueue(
            organization_id=current["organization_id"],
            kind="session.dispatch",
            payload={"session_id": current["session_id"]},
            idempotency_key=(
                f"session.dispatch.parallel_root:{task_id}:{revision}"
            ),
        )
        return {"task_id": task_id, "state": "queued", "prompt": arguments["prompt"]}

    # ---- manage_child_task: revise or cancel an existing child (plan §9.2) ----

    @staticmethod
    def tool_definition_manage() -> ToolDefinition:
        return ToolDefinition(
            name="manage_child_task",
            description=(
                "Cancel or revise one of this Task's own child Tasks "
                "(created via delegate_tasks). Cancelling stops it -- "
                "gracefully for a pi child (SIGTERM, then SIGKILL after a "
                "grace period if needed), immediately for a general child "
                "-- and preserves any workspace changes a pi child already "
                "made. Revising updates the child's plain-language prompt: "
                "a general child picks it up at its own next reasoning "
                "step; a pi child finishes or is cancelled first, then "
                "automatically restarts a new attempt with the revised "
                "prompt from the same persistent workspace -- you don't "
                "need to delegate a new task or poll for the restart "
                "yourself. Use this instead of creating a new child when "
                "an active one just needs different instructions or needs "
                "to stop."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "child_task_id": {"type": "string"},
                    "action": {"type": "string", "enum": ["cancel", "revise"]},
                    "reason": {"type": "string"},
                    "revised_prompt": {
                        "type": "string",
                        "description": "Required when action is 'revise'.",
                    },
                },
                "required": ["child_task_id", "action"],
                "additionalProperties": False,
            },
            risk="read",
        )

    def manage(
        self, arguments: dict[str, Any], runtime: dict[str, Any]
    ) -> dict[str, Any]:
        parent = self.store.get_task(runtime["task_id"])
        if not parent:
            raise KeyError(runtime["task_id"])
        child_id = arguments["child_task_id"]
        child = self.store.get_task(child_id)
        if not child or child.get("parent_task_id") != parent["id"]:
            raise ValueError(f"{child_id} is not a child of this task")
        action = arguments["action"]
        reason = arguments.get("reason") or "session manager requested"
        if action == "cancel":
            return self._cancel_child(child, reason=reason)
        if action == "revise":
            revised_prompt = arguments.get("revised_prompt")
            if not revised_prompt:
                raise ValueError("revise requires revised_prompt")
            return self._revise_child(child, revised_prompt=revised_prompt, reason=reason)
        raise ValueError(f"unsupported action: {action!r}")

    def _cancel_child(self, child: dict[str, Any], *, reason: str) -> dict[str, Any]:
        if child["state"] in _TERMINAL_TASK_STATES:
            return {"child_task_id": child["id"], "state": child["state"], "note": "already terminal"}
        if child.get("worker_type") == "pi":
            if not self.pi_executor:
                raise RuntimeError("no PiTaskExecutor configured for worker_type=pi")
            self.pi_executor.cancel(child["id"], reason=reason)
            return {"child_task_id": child["id"], "state": "cancelling", "reason": reason}
        # General children: cancellation is unconditional and immediate --
        # see storage/db.py's cancel_task() docstring for why this is
        # race-safe even against a concurrently-running _loop().
        self.store.cancel_task(child["id"])
        return {"child_task_id": child["id"], "state": AgentRunState.CANCELLED.value}

    def _revise_child(
        self, child: dict[str, Any], *, revised_prompt: str, reason: str
    ) -> dict[str, Any]:
        if child["state"] in _TERMINAL_TASK_STATES:
            return {"child_task_id": child["id"], "state": child["state"], "note": "already terminal"}
        now = utcnow().isoformat()
        child["prompt"] = revised_prompt
        child["goal"] = revised_prompt
        if child.get("worker_type") == "pi":
            if not self.pi_executor:
                raise RuntimeError("no PiTaskExecutor configured for worker_type=pi")
            metadata = dict(child.get("metadata") or {})
            metadata["pending_revision"] = True
            child["metadata"] = metadata
            child["updated_at"] = now
            self.store.update_task(child)
            self.store.bump_task_revision(child["id"])
            if child["state"] == AgentRunState.RUNNING.value:
                # Pi has no documented interactive redirect protocol (plan
                # §10.5) -- cancel now; PiTaskExecutor._finalize() sees
                # pending_revision once that cancellation is confirmed and
                # restarts automatically with the new prompt, same workspace.
                self.pi_executor.cancel(child["id"], reason=reason)
                return {"child_task_id": child["id"], "state": "revising", "note": "restarting with new prompt once current attempt stops"}
            # Not actively running yet (queued) -- nothing to cancel, the
            # next start() picks up the already-revised prompt directly.
            return {"child_task_id": child["id"], "state": child["state"], "note": "revised prompt will be used on next start"}
        # General child: live redirect via the same signal _loop() already
        # checks every iteration -- no cancel/restart needed.
        child["updated_at"] = now
        self.store.update_task(child)
        self.store.bump_task_revision(child["id"])
        self.store.push_task_signal(
            {
                "id": new_id("tsig"),
                "organization_id": child["organization_id"],
                "task_id": child["id"],
                "kind": "steer",
                "message": revised_prompt,
                "created_at": now,
            }
        )
        return {"child_task_id": child["id"], "state": child["state"], "note": "revision delivered"}
