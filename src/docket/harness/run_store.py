"""Durable session, run, transcript, checkpoint, and approval persistence."""

from __future__ import annotations

from typing import Any

from docket.domain.models import new_id, utcnow
from docket.harness.models import AgentRunState, InvocationEnvelope, TriggerKind
from docket.storage.db import BrainStore


_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    AgentRunState.CREATED.value: {
        AgentRunState.QUEUED.value,
        AgentRunState.RUNNING.value,
        AgentRunState.CANCELLED.value,
    },
    AgentRunState.QUEUED.value: {
        AgentRunState.RUNNING.value,
        AgentRunState.CANCELLED.value,
        AgentRunState.FAILED.value,
    },
    AgentRunState.RUNNING.value: {
        AgentRunState.WAITING_FOR_TOOL.value,
        AgentRunState.WAITING_FOR_APPROVAL.value,
        AgentRunState.WAITING_FOR_HUMAN.value,
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
    AgentRunState.SCHEDULED.value: {
        AgentRunState.QUEUED.value,
        AgentRunState.CANCELLED.value,
    },
    AgentRunState.PAUSED.value: {
        AgentRunState.RUNNING.value,
        AgentRunState.CANCELLED.value,
    },
    AgentRunState.FAILED.value: {
        AgentRunState.QUEUED.value,
        AgentRunState.CANCELLED.value,
    },
}


class AgentRunStore:
    def __init__(self, store: BrainStore) -> None:
        self.store = store

    def get_or_create_session(
        self,
        *,
        organization_id: str,
        conversation_key: str,
        destination: dict[str, Any],
        participants: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = self.store.session_by_conversation(
            organization_id, conversation_key
        )
        if existing:
            existing["participants"] = sorted(
                set(existing.get("participants") or []) | set(participants or [])
            )
            existing["updated_at"] = utcnow().isoformat()
            self.store.upsert_agent_session(existing)
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
        self.store.upsert_agent_session(session)
        return session

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
        run = {
            "id": new_id("arun"),
            "organization_id": envelope.organization_id,
            "session_id": session_id,
            "trigger_kind": trigger,
            "trigger_event_id": trigger_event_id,
            "parent_run_id": parent_run_id,
            "state": AgentRunState.CREATED.value,
            "goal": goal,
            "envelope": envelope.model_dump(mode="json"),
            "trace_id": envelope.trace_id,
            "max_iterations": max_iterations,
            "max_tool_calls": max_tool_calls,
            "iteration_count": 0,
            "tool_call_count": 0,
            "final_answer": None,
            "error": None,
            "events": [
                {
                    "type": "run.created",
                    "at": now,
                    "trigger_kind": trigger,
                    "trigger_event_id": trigger_event_id,
                }
            ],
            "metadata": metadata or {},
            "created_at": now,
            "updated_at": now,
        }
        self.store.upsert_agent_run(run)
        self.checkpoint(run, reason="created")
        return run

    def get(self, run_id: str) -> dict[str, Any] | None:
        return self.store.get_harness_record("agent_runs", run_id)

    def push_signal(
        self,
        run_id: str,
        organization_id: str,
        *,
        kind: str,
        message: str | None = None,
    ) -> dict[str, Any]:
        return self.store.push_agent_run_signal(
            {
                "id": new_id("rsig"),
                "organization_id": organization_id,
                "run_id": run_id,
                "kind": kind,
                "message": message,
                "created_at": utcnow().isoformat(),
            }
        )

    def pop_signal(self, run_id: str) -> dict[str, Any] | None:
        return self.store.pop_agent_run_signal(run_id)

    def transition(
        self,
        run_id: str,
        state: AgentRunState | str,
        *,
        event_type: str | None = None,
        event_payload: dict[str, Any] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        run = self.get(run_id)
        if not run:
            raise KeyError(f"run not found: {run_id}")
        target = state.value if isinstance(state, AgentRunState) else str(state)
        current = run["state"]
        if current == target:
            return run
        if not force and target not in _ALLOWED_TRANSITIONS.get(current, set()):
            raise ValueError(f"invalid run transition {current} -> {target}")
        now = utcnow().isoformat()
        run["state"] = target
        run["updated_at"] = now
        run.setdefault("events", []).append(
            {
                "type": event_type or f"run.{target}",
                "at": now,
                **(event_payload or {}),
            }
        )
        self.store.upsert_agent_run(run)
        self.checkpoint(run, reason=event_type or target)
        return run

    def update(self, run: dict[str, Any], *, checkpoint_reason: str | None = None) -> None:
        run["updated_at"] = utcnow().isoformat()
        self.store.upsert_agent_run(run)
        if checkpoint_reason:
            self.checkpoint(run, reason=checkpoint_reason)

    def checkpoint(self, run: dict[str, Any], *, reason: str) -> dict[str, Any]:
        checkpoints = self.store.list_harness_records(
            "checkpoints", run["organization_id"], where={"run_id": run["id"]}
        )
        item = {
            "id": new_id("checkpoint"),
            "organization_id": run["organization_id"],
            "run_id": run["id"],
            "sequence": len(checkpoints) + 1,
            "state": run["state"],
            "iteration_count": run.get("iteration_count", 0),
            "tool_call_count": run.get("tool_call_count", 0),
            "reason": reason,
            "created_at": utcnow().isoformat(),
        }
        with self.store.cursor() as cur:
            cur.execute(
                """INSERT INTO checkpoints
                (id, organization_id, run_id, sequence, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    item["id"],
                    item["organization_id"],
                    item["run_id"],
                    item["sequence"],
                    __import__("json").dumps(item),
                    item["created_at"],
                ),
            )
        return item

    def add_message(
        self,
        *,
        organization_id: str,
        session_id: str,
        run_id: str | None,
        role: str,
        content: str,
        tool_call_id: str | None = None,
        name: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        message = {
            "id": new_id("msg"),
            "organization_id": organization_id,
            "session_id": session_id,
            "run_id": run_id,
            "role": role,
            "content": content,
            "tool_call_id": tool_call_id,
            "name": name,
            "tool_calls": tool_calls or [],
            "metadata": metadata or {},
            "created_at": utcnow().isoformat(),
        }
        self.store.append_agent_message(message)
        return message

    def messages(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.session_messages(session_id, limit=limit)

    def append_turn(
        self,
        run: dict[str, Any],
        *,
        request: dict[str, Any],
        response: dict[str, Any],
    ) -> dict[str, Any]:
        turn_number = int(run.get("iteration_count", 0))
        turn = {
            "id": new_id("turn"),
            "organization_id": run["organization_id"],
            "run_id": run["id"],
            "turn_number": turn_number,
            "request": request,
            "response": response,
            "created_at": utcnow().isoformat(),
        }
        self.store.append_agent_turn(turn)
        return turn

    def complete(self, run_id: str, answer: str) -> dict[str, Any]:
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        run["final_answer"] = answer
        self.update(run)
        return self.transition(run_id, AgentRunState.COMPLETED)

    def fail(self, run_id: str, error: str) -> dict[str, Any]:
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        run["error"] = error
        self.update(run)
        return self.transition(
            run_id,
            AgentRunState.FAILED,
            event_payload={"error": error},
            force=(
                AgentRunState.FAILED.value
                not in _ALLOWED_TRANSITIONS.get(run["state"], set())
            ),
        )
