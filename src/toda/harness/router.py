"""Deterministic router connecting company events to memory and attention."""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from typing import Any

from toda.harness.event_policy import resolve_event_policy
from toda.harness.models import NormalizedEvent

log = logging.getLogger("toda.router")

# Slack has no public "bot is typing" indicator for the Web API (that's an
# RTM-only affordance), so the closest equivalent to "we're alive and
# working on it" is reacting to the triggering message immediately -- long
# before the agent loop that follows could take anywhere from one second to
# several minutes. Cleared by DeliveryService once the real answer (or a
# failure notice) actually posts.

_STOP_PHRASES = (
    "stop",
    "cancel",
    "abort",
    "never mind",
    "nevermind",
    "halt",
    "forget it",
)


def _looks_like_stop(text: str) -> bool:
    """Deterministic keyword check for a message that means "stop the run",
    not a full intent classifier — errs toward steer (the safer default;
    a mis-detected steer just adds context, a mis-detected abort throws away
    real work) rather than expanding this into model-based intent detection.
    """
    lower = (text or "").strip().lower()
    if not lower:
        return False
    return any(
        lower == phrase or lower.startswith(phrase + " ") or lower.startswith(phrase + ",")
        for phrase in _STOP_PHRASES
    )


class EventRouter:
    def __init__(
        self,
        *,
        store: Any,
        ingestion: Any,
        attention: Any,
        queue: Any,
        audit: Any,
        graph: Any,
        config: Any,
        sessions: Any,
        slack_sender: Any | None = None,
        slack_media: Any | None = None,
    ) -> None:
        self.store = store
        self.ingestion = ingestion
        self.attention = attention
        self.queue = queue
        self.audit = audit
        self.graph = graph
        self.config = config
        self.sessions = sessions
        self.slack_sender = slack_sender
        self.slack_media = slack_media

    def route(self, event_payload: dict[str, Any]) -> dict[str, Any]:
        event = NormalizedEvent(**event_payload)
        if not event.payload.get("observe_enabled", True):
            return {"route": "discarded", "reason": "observation_disabled"}
        if self.slack_media and event.source == "slack" and event.payload.get("files"):
            # This executes in the queued route job, never in the webhook ACK.
            # Attachment failures are converted to visible event context by
            # the ingestor instead of dropping the user's message.
            try:
                event = self.slack_media.enrich(event)
            except Exception:
                log.warning("Slack media enrichment failed for event_id=%s", event.id, exc_info=True)
                event.payload["text"] = "\n\n".join(
                    part for part in [str(event.payload.get("text") or "").strip(), "Slack attachments were present but could not be ingested."] if part
                )
        policy = resolve_event_policy(event)
        # Fast path only: redact, hash, version, write the Source row. No
        # embedding call, no model extraction, no reality/graph expansion —
        # those are slow/remote and must never sit ahead of wake (plan §2
        # Phase A). They run in the knowledge lane, enqueued below.
        source = self._persist_evidence(event, policy)
        # The one inbound write into the conversation log. Every Slack
        # message passes through here -- direct requests and ambient
        # channel chatter alike -- so the log stays a complete, ordered
        # record of the room rather than only the parts that woke Toda.
        self.store.append_conversation_message(
            organization_id=event.organization_id,
            workspace_id=event.scope.workspace_id or "",
            channel_id=event.scope.channel_id or "",
            role="user",
            text=str(event.payload.get("text") or ""),
            author=event.actor.id,
            message_id=event.id,
            at=event.occurred_at.isoformat(),
        )
        if event.is_approval_action:
            return {"route": "approval", "source_id": source["id"]}

        self._enqueue_knowledge_jobs(event, source, policy)

        if event.is_direct_agent_request or policy.wake_policy == "always":
            result = self._route_reactive(event)
            result["source_id"] = source["id"]
            return result
        if policy.wake_policy == "never":
            return {"route": "observed", "source_id": source["id"]}
        job = self.queue.enqueue(
            organization_id=event.organization_id,
            kind="attention.evaluate",
            payload={"event": event.model_dump(mode="json")},
            idempotency_key=f"attention:{event.id}",
        )
        return {
            "route": "ambient_attention",
            "source_id": source["id"],
            "job_id": job["id"],
        }

    def _enqueue_knowledge_jobs(
        self, event: NormalizedEvent, source: dict[str, Any], policy: Any
    ) -> None:
        """Background knowledge processing (plan §2 Phase B) — chunking,
        embedding, extraction, episode membership. Always enqueued so
        evidence stays searchable; never awaited by the caller."""
        self.queue.enqueue(
            organization_id=event.organization_id,
            kind="knowledge.index_evidence",
            payload={"source_id": source["id"], "memory_policy": policy.memory_policy},
            idempotency_key=f"knowledge.index_evidence:{source['id']}",
        )
        if policy.memory_policy == "episode" and policy.group_key:
            self.queue.enqueue(
                organization_id=event.organization_id,
                kind="knowledge.consolidate_episode",
                payload={
                    "organization_id": event.organization_id,
                    "group_key": policy.group_key,
                    "group_kind": policy.group_kind,
                    "source_system": event.source,
                    "scope_id": event.scope.scope_id,
                    "quiet_seconds": self.config.worker.episode_quiet_seconds,
                },
                idempotency_key=f"episode:{policy.group_key}:{event.id}",
                available_at=(
                    event.occurred_at + timedelta(seconds=self.config.worker.episode_quiet_seconds)
                ).isoformat(),
            )


    def _route_reactive(self, event: NormalizedEvent) -> dict[str, Any]:
        """Direct events (DMs, explicit mentions, interactive answers) wake
        the relevant Session Manager directly (plan §7.1). sessions.wake()
        already handles message coalescing, revision bumping, and steering
        (folding a message into whatever Task tree is already active, or
        cancelling it outright on an explicit stop -- plan §9); this only
        needs to resolve the destination and schedule the dispatch tick."""
        destination = {
            "type": "dm"
            if event.type == "slack.dm"
            else "private_channel"
            if event.scope.visibility != "organization_public"
            else "slack_channel",
            "id": event.scope.channel_id or "",
            "is_private": event.scope.visibility != "organization_public",
        }
        text = event.payload.get("text") or ""
        message_ts = event.payload.get("message_ts")
        slack_metadata = {
            "workspace_id": event.scope.workspace_id,
            "channel_id": event.scope.channel_id,
            "context_key": event.payload.get("context_key"),
            # Carried onto the root Task the same way channel_id/
            # workspace_id already are (session_manager.py's
            # _root_task_metadata copies this whole dict) so
            # DeliveryService can clear the ack reaction from the exact
            # message that started this Task, once its answer is ready.
            "message_ts": message_ts,
            # Routing/local-context metadata only. Session identity remains
            # workspace+channel regardless of this value.
            "thread_ts": event.payload.get("thread_ts"),
            "actor_profile": event.payload.get("actor_profile") or {
                "principal_id": event.actor.id,
                "source_user_id": event.actor.source_id,
                "display_name": event.actor.id,
            },
        }
        wake = self.sessions.wake(
            organization_id=event.organization_id,
            conversation_key=event.payload["conversation_key"],
            destination=destination,
            role="user",
            content=text,
            principal_id=event.actor.id,
            metadata={"slack": slack_metadata},
            message_metadata={
                "trigger_event_id": event.id,
                # A channel Session holds every participant's turns, so each
                # one carries its speaker into the prompt transcript.
                "author_name": (slack_metadata.get("actor_profile") or {}).get(
                    "display_name"
                )
                or event.actor.id,
                "thread_ts": event.payload.get("thread_ts"),
            },
        )
        job = self.queue.enqueue(
            organization_id=event.organization_id,
            kind="session.dispatch",
            payload={"session_id": wake["session_id"]},
            idempotency_key=f"session.dispatch:{wake['session_id']}:{wake['revision']}",
        )
        log.info(
            "Session woken: event_id=%s session_id=%s revision=%s signal=%s job_id=%s",
            event.id,
            wake["session_id"],
            wake["revision"],
            wake.get("steer_signal"),
            job["id"],
        )
        return {
            "route": "session_wake",
            "session_id": wake["session_id"],
            "revision": wake["revision"],
            "steer_signal": wake.get("steer_signal"),
            "job_id": job["id"],
        }

    def _persist_evidence(self, event: NormalizedEvent, policy: Any) -> dict[str, Any]:
        """Fast, local-only write of the canonical evidence row. No chunking,
        embedding, extraction, or reality/graph expansion here — see
        _enqueue_knowledge_jobs for the async continuation (plan §2)."""
        source = self.ingestion.persist(
            organization_id=event.organization_id,
            source_system=event.source,
            source_type=event.type,
            external_id=event.id,
            content=event.payload.get("text") or "",
            scope_id=event.scope.scope_id,
            author_entity_id=event.actor.id,
            channel_id=event.scope.channel_id,
            is_private_channel=event.scope.visibility == "private_channel",
            entity_ids=[],
            metadata={
                "workspace_id": event.scope.workspace_id,
                "channel_id": event.scope.channel_id,
                "conversation_key": event.payload.get("conversation_key"),
                "context_key": event.payload.get("context_key"),
                "message_ts": event.payload.get("message_ts"),
                "thread_ts": event.payload.get("thread_ts"),
                "event_id": event.id,
                "source": event.source,
                "trigger_slug": event.payload.get("trigger_slug"),
                "trigger_id": event.payload.get("trigger_id"),
                "connected_account_id": event.payload.get(
                    "connected_account_id"
                ),
                "raw_data": event.payload.get("raw_data"),
                # Indexed onto sources.group_key by BrainStore.upsert_source
                # so episode consolidation can look up "everything new in
                # this group" without scanning the org's whole history.
                "group_key": policy.group_key,
                "group_kind": policy.group_kind,
            },
            occurred_at=event.occurred_at,
        )
        self._update_event_graph(event, source)
        self.audit.record(
            event.organization_id,
            "event.routed",
            {
                "event_id": event.id,
                "direct": event.is_direct_agent_request,
                "source_id": source["id"],
            },
            principal_id=event.actor.id,
        )
        return source

    def _update_event_graph(
        self, event: NormalizedEvent, source: dict[str, Any]
    ) -> None:
        key = event.payload.get("context_key") or event.payload["conversation_key"]
        digest = hashlib.sha256(key.encode()).hexdigest()[:20]
        conversation_id = f"conversation_{digest}"
        self.graph.upsert_entity(
            {
                "id": conversation_id,
                "organization_id": event.organization_id,
                "entity_type": "conversation",
                "name": (
                    f"{event.source} {event.scope.channel_id}"
                ),
                "aliases": [key],
                "attributes": {
                    "workspace_id": event.scope.workspace_id,
                    "channel_id": event.scope.channel_id,
                    "scope_id": event.scope.scope_id,
                },
            }
        )
        self.graph.upsert_entity(
            {
                "id": source["id"],
                "organization_id": event.organization_id,
                "entity_type": "source",
                "name": f"{event.source} event {event.id}",
                "aliases": [],
                "attributes": {
                    "external_id": event.id,
                    "conversation_id": conversation_id,
                },
            }
        )
        self.graph.add_edge(
            event.organization_id,
            "PARTICIPATED_IN",
            event.actor.id,
            conversation_id,
            source_ids=[source["id"]],
        )
        self.graph.add_edge(
            event.organization_id,
            "MENTIONED_IN",
            source["id"],
            conversation_id,
            source_ids=[source["id"]],
        )
        lower = (event.payload.get("text") or "").lower()
        for entity in self.store.find_entity_candidates(
            event.organization_id, event.payload.get("text") or "", limit=50
        ):
            if entity["id"] in {
                event.actor.id,
                conversation_id,
                source["id"],
            }:
                continue
            names = [entity.get("name") or "", *(entity.get("aliases") or [])]
            if any(name and name.lower() in lower for name in names):
                self.graph.add_edge(
                    event.organization_id,
                    "MENTIONED_IN",
                    entity["id"],
                    conversation_id,
                    source_ids=[source["id"]],
                )
