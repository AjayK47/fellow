"""Task persistence adapter for the CompanyAgent reasoning loop.

All durable records are stored in the Session/Task schema; the adapter owns
leases, attempts, messages, turns, approvals, and checkpoints for one Task
executor.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from docket.domain.models import new_id, utcnow
from docket.harness.models import AgentRunState, InvocationEnvelope, TriggerKind
from docket.storage.db import BrainStore


_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    AgentRunState.QUEUED.value: {
        AgentRunState.RUNNING.value,
        AgentRunState.CANCELLED.value,
        AgentRunState.FAILED.value,
    },
    AgentRunState.RUNNING.value: {
        AgentRunState.WAITING_FOR_TOOL.value,
        AgentRunState.WAITING_FOR_APPROVAL.value,
        AgentRunState.WAITING_FOR_HUMAN.value,
        AgentRunState.WAITING_FOR_CHILDREN.value,
        AgentRunState.PAUSED.value,
        AgentRunState.COMPLETED.value,
        AgentRunState.FAILED.value,
        AgentRunState.CANCELLED.value,
    },
    AgentRunState.WAITING_FOR_TOOL.value: {
        AgentRunState.RUNNING.value,
        AgentRunState.FAILED.value,
        AgentRunState.CANCELLED.value,
    },
    AgentRunState.WAITING_FOR_APPROVAL.value: {
        AgentRunState.RUNNING.value,
        AgentRunState.CANCELLED.value,
        AgentRunState.FAILED.value,
    },
    AgentRunState.WAITING_FOR_HUMAN.value: {
        AgentRunState.RUNNING.value,
        AgentRunState.CANCELLED.value,
    },
    AgentRunState.WAITING_FOR_CHILDREN.value: {
        AgentRunState.RUNNING.value,
        AgentRunState.CANCELLED.value,
        AgentRunState.FAILED.value,
    },
    AgentRunState.PAUSED.value: {
        AgentRunState.RUNNING.value,
        AgentRunState.CANCELLED.value,
    },
    AgentRunState.FAILED.value: {AgentRunState.QUEUED.value},
}


class _TaskStoreProxy:
    """Expose the small storage surface used directly by CompanyAgent."""

    def __init__(self, store: BrainStore) -> None:
        object.__setattr__(self, "_store", store)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def get_harness_record(self, table: str, id_: str) -> dict[str, Any] | None:
        if table == "sessions":
            return self._store.get_session(id_)
        return self._store.get_harness_record(table, id_)

class TaskRunStore:
    """Task persistence adapter. `owner` identifies the
    current executor for claim/release/lease-renewal; construct a fresh
    instance (or call `bind`) per execution attempt so the right owner
    guards every claim/release call."""

    def __init__(
        self,
        store: BrainStore,
        *,
        owner: str,
        lease_seconds: int = 3600,
        lease_heartbeat: Callable[[], bool] | None = None,
    ) -> None:
        self.store = _TaskStoreProxy(store)
        self._raw_store = store
        self.owner = owner
        self.lease_seconds = lease_seconds
        self.lease_heartbeat = lease_heartbeat

    def bind(self, *, owner: str, lease_seconds: int | None = None) -> "TaskRunStore":
        return TaskRunStore(
            self._raw_store,
            owner=owner,
            lease_seconds=lease_seconds or self.lease_seconds,
            lease_heartbeat=self.lease_heartbeat,
        )

    def _root_of(self, task_id: str) -> str:
        parent = self._raw_store.get_task(task_id)
        return (parent or {}).get("root_task_id") or task_id

    def get_or_create_session(
        self,
        *,
        organization_id: str,
        conversation_key: str,
        destination: dict[str, Any],
        participants: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = self._raw_store.find_session_by_conversation(
            organization_id, conversation_key
        )
        if existing:
            if participants:
                existing["participants"] = sorted(
                    set(existing.get("participants") or []) | set(participants)
                )
                existing["updated_at"] = utcnow().isoformat()
                self._raw_store.update_session(existing)
            return existing
        now = utcnow().isoformat()
        session = {
            "id": new_id("session"),
            "organization_id": organization_id,
            "conversation_key": conversation_key,
            "destination": destination,
            "participants": participants or [],
            "state": "active",
            "summary": "",
            "metadata": metadata or {},
            "created_at": now,
            "updated_at": now,
        }
        return self._raw_store.create_session(session)

    def create_run(
        self,
        *,
        session_id: str,
        envelope: InvocationEnvelope,
        goal: str,
        trigger_kind: TriggerKind | str,
        trigger_event_id: str | None = None,
        parent_run_id: str | None = None,
        max_iterations: int = 12,
        max_tool_calls: int = 24,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utcnow().isoformat()
        trigger = (
            trigger_kind.value
            if isinstance(trigger_kind, TriggerKind)
            else str(trigger_kind)
        )
        task_id = new_id("task")
        root_task_id = self._root_of(parent_run_id) if parent_run_id else task_id
        worker_type = (metadata or {}).get("worker_type") or (
            "general" if parent_run_id else "session_manager"
        )
        task = {
            "id": task_id,
            "organization_id": envelope.organization_id,
            "session_id": session_id,
            "parent_task_id": parent_run_id,
            "root_task_id": root_task_id,
            # The target Task state machine (queued/running/waiting_*/
            # terminal) has no separate creation stage
            # did -- a freshly created Task is immediately claimable.
            "state": AgentRunState.QUEUED.value,
            "worker_type": worker_type,
            "trigger_kind": trigger,
            "trigger_event_id": trigger_event_id,
            "prompt": goal,
            "goal": goal,
            "envelope": envelope.model_dump(mode="json"),
            "trace_id": envelope.trace_id,
            "max_iterations": max_iterations,
            "max_tool_calls": max_tool_calls,
            "iteration_count": 0,
            "tool_call_count": 0,
            "final_answer": None,
            "result": None,
            "error": None,
            "events": [
                {
                    "type": "task.created",
                    "at": now,
                    "trigger_kind": trigger,
                    "trigger_event_id": trigger_event_id,
                }
            ],
            "metadata": metadata or {},
            "created_at": now,
            "updated_at": now,
        }
        self._raw_store.create_task(task)
        return task

    def get(self, run_id: str) -> dict[str, Any] | None:
        return self._raw_store.get_task(run_id)

    def push_signal(
        self, run_id: str, organization_id: str, *, kind: str, message: str | None = None
    ) -> dict[str, Any]:
        return self._raw_store.push_task_signal(
            {
                "id": new_id("tsig"),
                "organization_id": organization_id,
                "task_id": run_id,
                "kind": kind,
                "message": message,
                "created_at": utcnow().isoformat(),
            }
        )

    def pop_signal(self, run_id: str) -> dict[str, Any] | None:
        return self._raw_store.pop_task_signal(run_id)

    def transition(
        self,
        run_id: str,
        state: AgentRunState | str,
        *,
        event_type: str | None = None,
        event_payload: dict[str, Any] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        task = self.get(run_id)
        if not task:
            raise KeyError(f"task not found: {run_id}")
        target = state.value if isinstance(state, AgentRunState) else str(state)
        current = task["state"]
        if current == target:
            return task
        if (
            current == AgentRunState.FAILED.value
            and target == AgentRunState.QUEUED.value
        ):
            if not self._raw_store.retry_task(run_id):
                raise RuntimeError(f"failed to retry task {run_id}")
            return self.get(run_id) or task
        if not force and target not in _ALLOWED_TRANSITIONS.get(current, set()):
            raise ValueError(f"invalid task transition {current} -> {target}")
        now = utcnow().isoformat()
        task["state"] = target
        task["updated_at"] = now
        task.setdefault("events", []).append(
            {"type": event_type or f"task.{target}", "at": now, **(event_payload or {})}
        )
        if target == AgentRunState.RUNNING.value:
            claimed = self._raw_store.claim_task(
                run_id, owner=self.owner, lease_seconds=self.lease_seconds
            )
            if not claimed:
                # Someone else holds a live lease, or the task moved to a
                # terminal state underneath us -- surface this immediately
                # rather than let the real state column silently diverge
                # from the in-memory dict (the bug class that produced the
                # confusing downstream "invalid transition" errors before
                # claim_task's eligibility check was broadened).
                raise RuntimeError(
                    f"failed to claim task {run_id} while transitioning "
                    f"{current} -> {target}: owned by another executor or "
                    "already terminal"
                )
            if not self._raw_store.update_task_owned(task, owner=self.owner):
                raise RuntimeError(f"task lease lost while starting {run_id}")
        else:
            # Every transition _loop() makes away from RUNNING is
            # immediately followed by a `return` -- this is exactly the
            # release point, so leaving RUNNING always hands the lease back.
            released = self._raw_store.release_task(
                run_id, owner=self.owner, next_state=target, task=task
            )
            if not released:
                raise RuntimeError(
                    f"task lease lost while transitioning {run_id} to {target}"
                )
        return task

    def update(self, run: dict[str, Any], *, checkpoint_reason: str | None = None) -> None:
        run["updated_at"] = utcnow().isoformat()
        persisted = self.get(run["id"])
        if not persisted:
            raise KeyError(run["id"])
        if (
            persisted["state"] == AgentRunState.QUEUED.value
            and not persisted.get("execution_owner")
        ):
            if not self._raw_store.update_unclaimed_task(run):
                raise RuntimeError(
                    f"task was claimed while initializing {run['id']}"
                )
            return
        # Renew on every persisted iteration so a genuinely long-running
        # task's own executor never gets reclaimed out from under itself.
        renewed = self._raw_store.renew_task_lease(
            run["id"], owner=self.owner, lease_seconds=self.lease_seconds
        )
        if not renewed:
            raise RuntimeError(f"task lease lost while renewing {run['id']}")
        if self.lease_heartbeat is not None and not self.lease_heartbeat():
            raise RuntimeError("session manager lease lost")
        if not self._raw_store.update_task_owned(run, owner=self.owner):
            raise RuntimeError(f"task lease lost while updating {run['id']}")
        if checkpoint_reason:
            self.checkpoint(run, reason=checkpoint_reason)

    def renew(self, run_id: str) -> None:
        if not self._raw_store.renew_task_lease(
            run_id, owner=self.owner, lease_seconds=self.lease_seconds
        ):
            raise RuntimeError(f"task lease lost while renewing {run_id}")
        if self.lease_heartbeat is not None and not self.lease_heartbeat():
            raise RuntimeError("session manager lease lost")

    def checkpoint(self, run: dict[str, Any], *, reason: str) -> dict[str, Any]:
        checkpoints = self._raw_store.list_harness_records(
            "checkpoints", run["organization_id"], where={"task_id": run["id"]}
        )
        item = {
            "id": new_id("checkpoint"),
            "organization_id": run["organization_id"],
            "task_id": run["id"],
            "sequence": len(checkpoints) + 1,
            "state": run["state"],
            "iteration_count": run.get("iteration_count", 0),
            "tool_call_count": run.get("tool_call_count", 0),
            "reason": reason,
            "created_at": utcnow().isoformat(),
        }
        with self._raw_store.cursor() as cur:
            cur.execute(
                """INSERT INTO checkpoints
                (id, organization_id, task_id, sequence, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    item["id"],
                    item["organization_id"],
                    item["task_id"],
                    item["sequence"],
                    json.dumps(item),
                    item["created_at"],
                ),
            )
        return item

    def add_message(
        self,
        *,
        organization_id: str,
        session_id: str,
        task_id: str | None,
        role: str,
        content: str,
        tool_call_id: str | None = None,
        name: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        message = {
            "id": new_id("smsg"),
            "organization_id": organization_id,
            "session_id": session_id,
            "task_id": task_id,
            "role": role,
            "content": content,
            "tool_call_id": tool_call_id,
            "name": name,
            "tool_calls": tool_calls or [],
            "metadata": metadata or {},
            "created_at": utcnow().isoformat(),
        }
        self._raw_store.append_session_message(message)
        return message

    def messages(
        self,
        session_id: str,
        *,
        limit: int = 100,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._raw_store.list_session_messages(
            session_id, limit=limit, task_id=task_id
        )

    def append_turn(
        self, run: dict[str, Any], *, request: dict[str, Any], response: dict[str, Any]
    ) -> dict[str, Any]:
        turn_number = int(run.get("iteration_count", 0))
        turn = {
            "id": new_id("turn"),
            "organization_id": run["organization_id"],
            "task_id": run["id"],
            "turn_number": turn_number,
            "request": request,
            "response": response,
            "created_at": utcnow().isoformat(),
        }
        self._raw_store.append_task_turn(turn)
        return turn

    def complete(self, run_id: str, answer: str) -> dict[str, Any]:
        task = self.get(run_id)
        if not task:
            raise KeyError(run_id)
        task["final_answer"] = answer
        task["result"] = answer
        self.update(task)
        return self.transition(run_id, AgentRunState.COMPLETED)

    def fail(self, run_id: str, error: str) -> dict[str, Any]:
        task = self.get(run_id)
        if not task:
            raise KeyError(run_id)
        task["error"] = error
        self.update(task)
        return self.transition(
            run_id,
            AgentRunState.FAILED,
            event_payload={"error": error},
            force=(
                AgentRunState.FAILED.value
                not in _ALLOWED_TRANSITIONS.get(task["state"], set())
            ),
        )
