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

from typing import Any

from docket.domain.models import new_id, utcnow
from docket.harness.models import AgentRunState, ToolDefinition, TriggerKind

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
        pi_executor: Any = None,
        max_parallel: int = 3,
        max_depth: int = 2,
    ) -> None:
        self.store = store
        self.queue = queue
        self.audit = audit
        # Set post-construction in container.py -- PiTaskExecutor needs
        # `sessions`, which is built before ChildTaskService's own pi
        # dependency is available.
        self.pi_executor = pi_executor
        self.max_parallel = max(1, max_parallel)
        self.max_depth = max(1, max_depth)

    @staticmethod
    def tool_definition() -> ToolDefinition:
        return ToolDefinition(
            name="delegate_tasks",
            description=(
                "Create one to three child Tasks for parallel or specialized "
                "work: worker_type='general' for investigation/analysis/action, "
                "or worker_type='pi' for coding work. Give each child a "
                "complete, plain-language prompt -- no role, objective, or "
                "output contract schema needed. Children execute "
                "independently and concurrently; you are woken when a child "
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
        child_metadata: dict[str, Any] = {
            "worker_depth": depth + 1,
            "suppress_delivery": True,
            "suppress_memory_capture": True,
        }
        parent_slack = (parent.get("metadata") or {}).get("slack")
        if parent_slack:
            # Needed for WorkspaceService.get_for_task() to resolve the same
            # persistent workspace as the rest of this Task tree -- most
            # relevant for worker_type=pi children.
            child_metadata["slack"] = parent_slack
        created: list[dict[str, Any]] = []
        for spec in tasks:
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
                # Child workers inherit the parent's generous runaway guards;
                # specialization should not impose a tiny hidden runtime cap.
                "max_iterations": int(parent.get("max_iterations") or 100),
                "max_tool_calls": int(parent.get("max_tool_calls") or 150),
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
                    **child_metadata,
                    "worker_type": spec["worker_type"],
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
