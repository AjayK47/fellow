"""SessionManagerScheduler: claims a Session's Manager lock, drives its
root Task through the existing CompanyAgent reasoning loop (via
TaskRunStore), and releases the lock -- idle if every pending revision has
been processed, or immediately re-queued if new input arrived while it was
working. This is the "one active Session Manager per Session, many Sessions
concurrently" scheduler described in the Session/Task architecture plan.
"""

from __future__ import annotations

from typing import Any

from toda.domain.models import new_id, utcnow
from toda.harness.models import (
    AgentOutcome,
    AgentRunState,
    DestinationEnvelope,
    InvocationEnvelope,
    TriggerKind,
)
from toda.harness.retry import is_retryable_task_error
from toda.harness.task_run_store import TaskRunStore

# WAITING_FOR_APPROVAL is deliberately not resumable from here -- resolving
# an approval wakes the parent Task directly (Phase 7), not through a new
# Session message.
_RESUMABLE_HUMAN_STATES = {AgentRunState.WAITING_FOR_HUMAN.value}
_RESUMABLE_CHILD_STATES = {AgentRunState.WAITING_FOR_CHILDREN.value}
_TERMINAL_STATES = {
    AgentRunState.COMPLETED.value,
    AgentRunState.FAILED.value,
    AgentRunState.CANCELLED.value,
}


class SessionManagerScheduler:
    def __init__(
        self,
        *,
        store: Any,
        authz: Any,
        sessions: Any,
        agent_factory: Any,
        lease_seconds: int = 3600,
        task_lease_seconds: int = 3600,
    ) -> None:
        """`agent_factory(runs)` must return a CompanyAgent-compatible
        instance constructed with the given TaskRunStore as its `runs`
        dependency -- container.py supplies this so the scheduler doesn't
        need to know CompanyAgent's full constructor signature."""
        self.store = store
        self.authz = authz
        self.sessions = sessions
        self.agent_factory = agent_factory
        self.lease_seconds = lease_seconds
        self.task_lease_seconds = task_lease_seconds

    def dispatch(self, session_id: str) -> AgentOutcome | None:
        """Attempt to claim and run this Session's Manager. Returns None if
        someone else already owns it or there's nothing to process."""
        executor_id = new_id("exec")
        if not self.sessions.claim(
            session_id, owner=executor_id, lease_seconds=self.lease_seconds
        ):
            return None
        try:
            if not self.sessions.renew(
                session_id, owner=executor_id, lease_seconds=self.lease_seconds
            ):
                raise RuntimeError("session manager lease lost before execution")
            outcome = self._run_once(session_id, executor_id)
        except Exception:
            # Leave it queued (not idle) on an infrastructure failure so a
            # later tick retries rather than silently going quiet.
            self.sessions.release(session_id, owner=executor_id, idle=False)
            raise
        session = self.sessions.get(session_id)
        should_idle = session["processed_revision"] >= session["manager_revision"]
        self.sessions.release(session_id, owner=executor_id, idle=should_idle)
        return outcome

    def _run_once(self, session_id: str, executor_id: str) -> AgentOutcome:
        session = self.sessions.get(session_id)
        revision_at_start = session["manager_revision"]
        runs = TaskRunStore(
            self.store,
            owner=executor_id,
            lease_seconds=self.task_lease_seconds,
            lease_heartbeat=lambda: self.sessions.renew(
                session_id,
                owner=executor_id,
                lease_seconds=self.lease_seconds,
            ),
        )
        agent = self.agent_factory(runs)

        root_task = self.store.find_root_task_for_session(session_id)
        messages = self.sessions.messages(session_id, limit=5)
        latest_user_message = next(
            (m for m in reversed(messages) if m["role"] in {"user", "event"}), None
        )
        latest_user_text = (latest_user_message or {}).get("content") or ""
        trigger_event_id = (latest_user_message or {}).get("metadata", {}).get(
            "trigger_event_id"
        )
        wake_metadata = (latest_user_message or {}).get("metadata") or {}
        trigger_kind_value = wake_metadata.get("trigger_kind") or TriggerKind.REACTIVE.value
        try:
            trigger_kind = TriggerKind(trigger_kind_value)
        except ValueError:
            trigger_kind = TriggerKind.REACTIVE
        purpose = wake_metadata.get("purpose") or "reactive_agent"
        principal_id = session.get("last_actor_principal_id") or "unknown"
        destination = session["destination"]

        # Existing work may have been started by a different participant in a
        # shared channel. A wake never inherits that actor's authorization:
        # replace the Task envelope with a fresh decision before resuming it.
        if root_task is not None and (
            (root_task.get("envelope") or {}).get("principal_id") != principal_id
            or (root_task.get("envelope") or {}).get("purpose") != purpose
        ):
            refreshed = self._build_envelope(
                organization_id=session["organization_id"],
                principal_id=principal_id,
                conversation_id=session["conversation_key"],
                destination=destination,
                purpose=purpose,
            )
            root_task["envelope"] = refreshed.model_dump(mode="json")
            root_task["updated_at"] = utcnow().isoformat()
            self.store.update_task(root_task)

        if root_task is None:
            envelope = self._build_envelope(
                organization_id=session["organization_id"],
                principal_id=principal_id,
                conversation_id=session["conversation_key"],
                destination=destination,
                purpose=purpose,
            )
            root_task = runs.create_run(
                session_id=session_id,
                envelope=envelope,
                goal=latest_user_text,
                trigger_kind=trigger_kind,
                trigger_event_id=trigger_event_id,
                max_iterations=agent.max_iterations,
                max_tool_calls=agent.max_tool_calls,
                metadata=self._root_task_metadata(session),
            )
            outcome = agent.execute_task(root_task["id"])
        elif root_task["state"] == AgentRunState.QUEUED.value:
            outcome = agent.execute_task(root_task["id"])
        elif root_task["state"] in _RESUMABLE_HUMAN_STATES:
            outcome = agent.resume_with_human_message(
                root_task["id"],
                message=latest_user_text,
                principal_id=principal_id,
                persist_message=False,
            )
        elif root_task["state"] in _RESUMABLE_CHILD_STATES:
            outcome = agent.resume_for_children(root_task["id"])
        elif root_task["state"] in _TERMINAL_STATES:
            # Session revision advanced (new message) after the root Task
            # already finished -- start a fresh root Task for this turn.
            envelope = self._build_envelope(
                organization_id=session["organization_id"],
                principal_id=principal_id,
                conversation_id=session["conversation_key"],
                destination=destination,
                purpose=purpose,
            )
            root_task = runs.create_run(
                session_id=session_id,
                envelope=envelope,
                goal=latest_user_text,
                trigger_kind=trigger_kind,
                trigger_event_id=trigger_event_id,
                max_iterations=agent.max_iterations,
                max_tool_calls=agent.max_tool_calls,
                metadata=self._root_task_metadata(session),
            )
            outcome = agent.execute_task(root_task["id"])
        else:
            # queued/running/waiting_for_tool/waiting_for_approval -- not
            # independently resumable from here; leave it for the approval
            # flow or a later tick once it naturally reaches a stable state.
            return AgentOutcome(
                task_id=root_task["id"],
                state=AgentRunState(root_task["state"]),
                trace_id=root_task.get("trace_id", ""),
            )

        # A transient failure (rate limit, timeout, ...) must not be marked
        # processed -- that would permanently lose the request unless
        # something else happens to wake this Session again. Leaving
        # processed_revision behind keeps the Session `queued` (dispatch()'s
        # should_idle check below), so a retried "session.dispatch" job (or
        # any later tick) picks it back up. A non-retryable failure IS
        # marked processed -- it's final, nothing will resolve itself by
        # trying again unprompted.
        transient = outcome.state == AgentRunState.FAILED and is_retryable_task_error(
            outcome.error or ""
        )
        if not transient:
            self.sessions.mark_processed(session_id, revision_at_start)
        return outcome

    @staticmethod
    def _root_task_metadata(session: dict[str, Any]) -> dict[str, Any]:
        # Carries the Session's Slack workspace/channel info onto its root
        # Task so WorkspaceService.get_for_task() (keyed off
        # metadata["slack"]["workspace_id"]) resolves the right persistent
        # workspace -- needed for any worker_type=pi child this Task later
        # delegates (ChildTaskService.execute() inherits it from here).
        metadata: dict[str, Any] = {"worker_type": "session_manager"}
        session_metadata = session.get("metadata") or {}
        for key in ("slack", "role_skill_ids", "attention_decision"):
            if session_metadata.get(key):
                metadata[key] = session_metadata[key]
        return metadata

    def _build_envelope(
        self,
        *,
        organization_id: str,
        principal_id: str,
        conversation_id: str,
        destination: dict,
        purpose: str,
    ) -> InvocationEnvelope:
        # Every wake re-authorizes fresh against the current actor and
        # destination -- never a cached permission decision (plan §7.4,
        # §23 invariant #3). This is the exact same authz.resolve() call
        # EventRouter._route_reactive() already makes for direct events.
        decision = self.authz.resolve(
            organization_id=organization_id,
            acting_principal_id=principal_id,
            destination=destination,
            purpose=purpose,
        )
        return InvocationEnvelope(
            organization_id=organization_id,
            principal_id=principal_id,
            application_id="slack_toda",
            conversation_id=conversation_id,
            destination=DestinationEnvelope(**destination),
            purpose=purpose,
            permission_decision_id=decision.id,
            allowed_scope_ids=decision.allowed_scope_ids,
        )
