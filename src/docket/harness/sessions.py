"""SessionService: the Session half of the Session/Task architecture.

Wraps the low-level Session storage primitives (storage/db.py) with the
operations the event pipeline and the Session Manager scheduler actually
need: resolve-or-create a Session for a conversation, append a message and
bump its revision (the single "wake" entrypoint), and the atomic
claim/renew/release trio that gives every Session exactly one active
Manager at a time.

Phase 6 (plan §9): wake() also decides whether a new message should be
folded into whatever Task tree is already active for this Session --
mirroring EventRouter._route_reactive()'s decision, adapted to the
Task model's claim/lease columns: an explicit stop/cancel unconditionally
cancels the root Task and every active child immediately (no Manager model
call needed to interrupt consequential work -- plan §9.3), while an
ordinary steering message is pushed as a signal the root Task's own
_loop() picks up at its next iteration boundary if it's genuinely running
right now, or simply sees in the transcript on its next resume otherwise.
"""

from __future__ import annotations

from typing import Any

from docket.domain.models import new_id, utcnow
from docket.harness.router import _looks_like_stop

_DEFAULT_MANAGER_LEASE_SECONDS = 3600
_TERMINAL_TASK_STATES = {"completed", "failed", "cancelled"}


class SessionService:
    def __init__(
        self,
        *,
        store: Any,
        audit: Any,
        queue: Any = None,
        pi_executor: Any = None,
    ) -> None:
        self.store = store
        self.audit = audit
        self.queue = queue
        # Set post-construction in container.py (PiTaskExecutor needs
        # `sessions` itself for its own notify() calls -- same circular-ish
        # ordering used for the Session Manager and Pi executor).
        self.pi_executor = pi_executor

    def resolve(
        self,
        *,
        organization_id: str,
        conversation_key: str,
        destination: dict[str, Any],
        participants: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Get-or-create the one Session for this conversation key. Merges
        in new participants on repeat resolution; never touches
        manager_state/owner/lease/revision."""
        existing = self.store.find_session_by_conversation(
            organization_id, conversation_key
        )
        if existing:
            changed = False
            if participants:
                merged_participants = sorted(
                    set(existing.get("participants") or []) | set(participants)
                )
                if merged_participants != existing.get("participants"):
                    existing["participants"] = merged_participants
                    changed = True
            if metadata:
                merged_metadata = dict(existing.get("metadata") or {})
                for key, value in metadata.items():
                    if isinstance(value, dict) and isinstance(
                        merged_metadata.get(key), dict
                    ):
                        merged_metadata[key] = {
                            **merged_metadata[key],
                            **value,
                        }
                    else:
                        merged_metadata[key] = value
                if merged_metadata != existing.get("metadata"):
                    existing["metadata"] = merged_metadata
                    changed = True
            if changed:
                existing["updated_at"] = utcnow().isoformat()
                self.store.update_session(existing)
            return existing
        now = utcnow().isoformat()
        session = {
            "id": new_id("session"),
            "organization_id": organization_id,
            "conversation_key": conversation_key,
            "destination": destination,
            "participants": participants or [],
            "summary": "",
            "metadata": metadata or {},
            "created_at": now,
            "updated_at": now,
        }
        return self.store.create_session(session)

    def get(self, session_id: str) -> dict[str, Any] | None:
        return self.store.get_session(session_id)

    def update(self, session: dict[str, Any]) -> None:
        session["updated_at"] = utcnow().isoformat()
        self.store.update_session(session)

    def append_message(
        self,
        *,
        organization_id: str,
        session_id: str,
        role: str,
        content: str,
        task_id: str | None = None,
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
        self.store.append_session_message(message)
        return message

    def messages(self, session_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.list_session_messages(session_id, limit=limit)

    def wake(
        self,
        *,
        organization_id: str,
        conversation_key: str,
        destination: dict[str, Any],
        role: str,
        content: str,
        principal_id: str | None = None,
        participants: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        message_metadata: dict[str, Any] | None = None,
        trigger_kind: str = "reactive",
        purpose: str = "reactive_agent",
    ) -> dict[str, Any]:
        """The single entrypoint for "new input arrived for this
        conversation": resolve the Session, append the message, and bump
        its revision (which flips an idle Session to queued -- a
        currently-running one just sees the higher revision at its next
        iteration boundary). Coalesces any number of rapid messages into
        one Session with a growing transcript and a monotonically
        increasing revision, never a second concurrent Manager."""
        session = self.resolve(
            organization_id=organization_id,
            conversation_key=conversation_key,
            destination=destination,
            participants=participants,
            metadata=metadata,
        )
        if principal_id and session.get("last_actor_principal_id") != principal_id:
            # Every wake re-authorizes using the CURRENT event's actor, not
            # a cached identity from whoever started the session -- this is
            # only ever a hint for the scheduler to resolve authz fresh at
            # dispatch time, never treated as a standing permission grant.
            session["last_actor_principal_id"] = principal_id
            session["updated_at"] = utcnow().isoformat()
            self.store.update_session(session)
        wake_metadata = dict(message_metadata or {})
        # Trigger and permission purpose describe this specific wake, not a
        # permanent grant cached on the Session.  The scheduler reads them
        # from the latest user message and resolves authorization afresh.
        wake_metadata["trigger_kind"] = trigger_kind
        wake_metadata["purpose"] = purpose
        self.append_message(
            organization_id=organization_id,
            session_id=session["id"],
            role=role,
            content=content,
            metadata=wake_metadata,
        )
        revision = self.store.bump_session_revision(session["id"])
        signal_kind = (
            self._steer_active_tasks(session, content, principal_id=principal_id)
            if role == "user"
            else None
        )
        self.audit.record(
            organization_id,
            "session.woken",
            {
                "session_id": session["id"],
                "manager_revision": revision,
                "wake_role": role,
                "trigger_kind": trigger_kind,
                "purpose": purpose,
                "steer_signal": signal_kind,
            },
        )
        return {"session_id": session["id"], "revision": revision, "steer_signal": signal_kind}

    def _steer_active_tasks(
        self,
        session: dict[str, Any],
        content: str,
        *,
        principal_id: str | None,
    ) -> str | None:
        """Fold a new user message into whatever Task tree is already
        active for this Session, rather than letting it sit unseen until
        whichever dispatch call (if any) is currently in flight finishes on
        its own. Returns "abort"/"steer"/None (nothing active to steer)."""
        root = self.store.find_root_task_for_session(session["id"])
        if not root:
            return None
        if _looks_like_stop(content):
            self._cancel_task_tree(session["organization_id"], root, reason=content)
            return "abort"
        if principal_id and (root.get("envelope") or {}).get("principal_id") != principal_id:
            # Only an executor that is actively inside CompanyAgent._loop()
            # needs an out-of-band interrupt. Stable waiting/queued Tasks
            # will be refreshed by SessionManagerScheduler before resume;
            # leaving a reauthorize signal on those Tasks would make the
            # freshly-authorized resume immediately pause a second time.
            if root["state"] == "running":
                self.store.push_task_signal(
                    {
                        "id": new_id("tsig"),
                        "organization_id": session["organization_id"],
                        "task_id": root["id"],
                        "kind": "reauthorize",
                        "message": None,
                        "created_at": utcnow().isoformat(),
                    }
                )
            return "reauthorize"
        # An ordinary steering message only injects into the root's own
        # live loop if it's genuinely mid-iteration right now; children are
        # never touched mechanically here -- revising/cancelling a specific
        # child is the Manager's own judgment call (manage_child_task),
        # made once it wakes and reads this message in the transcript.
        if root["state"] == "running":
            self.store.push_task_signal(
                {
                    "id": new_id("tsig"),
                    "organization_id": session["organization_id"],
                    "task_id": root["id"],
                    "kind": "steer",
                    # The actual message is already durable in session_messages;
                    # the signal only interrupts the live iteration boundary.
                    "message": None,
                    "created_at": utcnow().isoformat(),
                }
            )
            return "steer"
        return None

    def _cancel_task_tree(self, organization_id: str, root: dict[str, Any], *, reason: str) -> None:
        """Explicit stop/cancel (plan §9.3): interrupt consequential work
        immediately, without waiting for a Manager model call. Direct,
        unconditional cancellation (not a signal) works here whether the
        root/child is genuinely running right now, merely queued, or
        paused -- see storage/db.py's cancel_task() docstring for why this
        is race-safe against a concurrently-running _loop()."""
        self.store.cancel_task(root["id"])
        for child in self.store.list_tasks(organization_id, parent_task_id=root["id"]):
            if child["state"] in _TERMINAL_TASK_STATES:
                continue
            if child.get("worker_type") == "pi" and self.pi_executor:
                self.pi_executor.cancel(child["id"], reason=reason)
            else:
                self.store.cancel_task(child["id"])

    def notify(self, session_id: str, *, reason: str) -> int:
        """Wake a Session from something other than a chat message -- a
        child Task's terminal transition, an event, a schedule, an approval
        (Phase 7). Same coalescing bump as wake(), just no transcript
        message appended, since there's no new conversational turn."""
        revision = self.store.bump_session_revision(session_id)
        session = self.get(session_id)
        if session:
            self.audit.record(
                session["organization_id"],
                "session.woken",
                {
                    "session_id": session_id,
                    "manager_revision": revision,
                    "wake_reason": reason,
                },
            )
            if self.queue is not None:
                self.queue.enqueue(
                    organization_id=session["organization_id"],
                    kind="session.dispatch",
                    payload={"session_id": session_id},
                    idempotency_key=f"session.dispatch:{session_id}:{revision}",
                )
        return revision

    def claim(self, session_id: str, *, owner: str, lease_seconds: int = _DEFAULT_MANAGER_LEASE_SECONDS) -> bool:
        return self.store.claim_session_manager(
            session_id, owner=owner, lease_seconds=lease_seconds
        )

    def renew(self, session_id: str, *, owner: str, lease_seconds: int = _DEFAULT_MANAGER_LEASE_SECONDS) -> bool:
        return self.store.renew_session_manager_lease(
            session_id, owner=owner, lease_seconds=lease_seconds
        )

    def release(self, session_id: str, *, owner: str, idle: bool) -> bool:
        return self.store.release_session_manager(session_id, owner=owner, idle=idle)

    def mark_processed(self, session_id: str, revision: int) -> None:
        self.store.mark_session_revision_processed(session_id, revision)
