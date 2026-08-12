"""ChildTaskExecutor: the job-queue-side counterpart to delegate_tasks.

Each child Task created by ChildTaskService is enqueued as a "task.run_child"
job. A worker_type="general" child runs the ordinary CompanyAgent reasoning
loop (agent_factory(task_store) -- a fresh CompanyAgent bound to a fresh
TaskRunStore per attempt, so sibling children and the parent never collide
on ownership); a worker_type="pi" child is not an LLM reasoning loop at all
-- per plan §10.1, Pi is a Task runtime, so this just claims and starts a
PiTaskExecutor attempt (durable, subprocess-backed, self-rescheduling via
"task.pi_poll" -- see pi_task_executor.py) instead.

On a general child's terminal transition, or a Pi attempt's terminal poll,
the owning Session is notified so the next scheduler dispatch can resume the
waiting parent via CompanyAgent.resume_for_children().
"""

from __future__ import annotations

from typing import Any

from toda.domain.models import new_id
from toda.harness.models import AgentRunState
from toda.harness.task_run_store import TaskRunStore

_TERMINAL_STATES = {
    AgentRunState.COMPLETED.value,
    AgentRunState.FAILED.value,
    AgentRunState.CANCELLED.value,
}


class ChildTaskExecutor:
    def __init__(
        self,
        *,
        store: Any,
        queue: Any,
        sessions: Any,
        agent_factory: Any,
        pi_executor: Any = None,
        lease_seconds: int = 1800,
    ) -> None:
        self.store = store
        self.queue = queue
        self.sessions = sessions
        self.agent_factory = agent_factory
        self.pi_executor = pi_executor
        self.lease_seconds = lease_seconds

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        task_id = payload["task_id"]
        task = self.store.get_task(task_id)
        if not task:
            raise KeyError(f"child task not found: {task_id}")
        if task.get("worker_type") == "pi":
            if not self.pi_executor:
                raise RuntimeError("no PiTaskExecutor configured for worker_type=pi")
            executor_id = new_id("exec")
            return self.pi_executor.start(
                task_id, owner=executor_id, enqueue_poll=self._enqueue_poll
            )
        executor_id = new_id("exec")
        runs = TaskRunStore(
            self.store, owner=executor_id, lease_seconds=self.lease_seconds
        )
        task = runs.get(task_id)
        agent = self.agent_factory(runs)
        if task["state"] == AgentRunState.QUEUED.value:
            outcome = agent.execute_task(task_id)
        elif task["state"] == AgentRunState.WAITING_FOR_CHILDREN.value:
            outcome = agent.resume_for_children(task_id)
        else:
            # Already terminal, mid-flight under another owner's live
            # lease, or paused on something this executor doesn't drive
            # (e.g. waiting_for_approval -- Phase 7 links that resume path
            # directly). Nothing to do this tick.
            return {"task_id": task_id, "state": task["state"]}
        if outcome.state == AgentRunState.WAITING_FOR_APPROVAL and outcome.approval_id:
            self.queue.enqueue(
                organization_id=task["organization_id"],
                kind="delivery.approval",
                payload={"approval_id": outcome.approval_id},
                idempotency_key=f"delivery.approval:{outcome.approval_id}",
            )
        session_id = task.get("session_id")
        parent = (
            self.store.get_task(task["parent_task_id"])
            if task.get("parent_task_id")
            else None
        )
        if (
            outcome.state.value in _TERMINAL_STATES
            and session_id
            and parent
            and parent["state"] not in _TERMINAL_STATES
        ):
            self.store.bump_task_revision(task_id)
            self.store.bump_task_revision(parent["id"])
            self.sessions.notify(
                session_id, reason=f"child_task.{outcome.state.value}"
            )
        return outcome.model_dump(mode="json")

    def poll_pi(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Handler for "task.pi_poll" jobs -- see PiTaskExecutor.poll()."""
        if not self.pi_executor:
            raise RuntimeError("no PiTaskExecutor configured for worker_type=pi")
        return self.pi_executor.poll(payload["task_id"], enqueue_poll=self._enqueue_poll)

    def _enqueue_poll(self, task_id: str, *, available_at: str | None) -> None:
        task = self.store.get_task(task_id)
        if not task:
            return
        self.queue.enqueue(
            organization_id=task["organization_id"],
            kind="task.pi_poll",
            payload={"task_id": task_id},
            # Each scheduled poll is logically a distinct future event, not
            # a retry of a prior one -- a fixed key would dedupe every poll
            # after the first into a no-op.
            idempotency_key=f"task.pi_poll:{task_id}:{new_id('tick')}",
            available_at=available_at,
        )
