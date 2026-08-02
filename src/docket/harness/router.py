"""Deterministic router connecting company events to memory and attention."""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from typing import Any

from docket.harness.models import (
    InvocationEnvelope,
    NormalizedEvent,
    TriggerKind,
)

log = logging.getLogger("docket.router")

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
        retrieval: Any,
        agent: Any,
        attention: Any,
        queue: Any,
        audit: Any,
        graph: Any,
        config: Any,
    ) -> None:
        self.store = store
        self.ingestion = ingestion
        self.retrieval = retrieval
        self.agent = agent
        self.attention = attention
        self.queue = queue
        self.audit = audit
        self.graph = graph
        self.config = config

    def route(self, event_payload: dict[str, Any]) -> dict[str, Any]:
        event = NormalizedEvent(**event_payload)
        if not event.payload.get("observe_enabled", True):
            return {"route": "discarded", "reason": "observation_disabled"}
        source = self._store_raw_evidence(event)
        if event.is_approval_action:
            return {"route": "approval", "source_id": source["id"]}
        if event.is_direct_agent_request:
            result = self._route_reactive(event)
            result["source_id"] = source["id"]
            return result
        job = self.queue.enqueue(
            organization_id=event.organization_id,
            kind="attention.evaluate",
            payload={"event": event.model_dump(mode="json")},
            idempotency_key=f"attention:{event.id}",
        )
        self.queue.enqueue(
            organization_id=event.organization_id,
            kind="memory.consolidate_episode",
            payload={
                "organization_id": event.organization_id,
                "conversation_key": event.payload.get("context_key")
                or event.payload["conversation_key"],
                "quiet_seconds": self.config.episode_quiet_seconds,
            },
            idempotency_key=(
                f"episode:{event.payload.get('context_key') or event.payload['conversation_key']}:{event.id}"
            ),
            available_at=(
                event.occurred_at
                + timedelta(seconds=self.config.episode_quiet_seconds)
            ).isoformat(),
        )
        return {
            "route": "ambient_attention",
            "source_id": source["id"],
            "job_id": job["id"],
        }

    def _route_reactive(self, event: NormalizedEvent) -> dict[str, Any]:
        destination = {
            "type": "dm"
            if event.type == "slack.dm"
            else "private_channel"
            if event.scope.visibility != "organization_public"
            else "slack_channel",
            "id": event.scope.channel_id or "",
            "is_private": event.scope.visibility != "organization_public",
        }
        authz = self.retrieval.authorize(
            event.organization_id,
            event.actor.id,
            destination,
            "reactive_agent",
        )
        envelope = InvocationEnvelope(
            organization_id=event.organization_id,
            principal_id=event.actor.id,
            application_id="slack_docket",
            conversation_id=event.payload["conversation_key"],
            destination=destination,
            purpose="reactive_agent",
            permission_decision_id=authz.id,
            allowed_scope_ids=authz.allowed_scope_ids,
        )
        existing_session = self.store.session_by_conversation(
            event.organization_id, event.payload["conversation_key"]
        )
        active = (
            self.store.active_run_for_session(existing_session["id"])
            if existing_session
            else None
        )
        text = event.payload.get("text") or ""
        if active and active["state"] == "waiting_for_human":
            job = self.queue.enqueue(
                organization_id=event.organization_id,
                kind="agent.human_resume",
                payload={
                    "run_id": active["id"],
                    "message": text,
                    "principal_id": event.actor.id,
                },
                idempotency_key=f"human.resume:{active['id']}:{event.id}",
            )
            return {
                "route": "human_continuation",
                "run_id": active["id"],
                "job_id": job["id"],
            }
        # A run that's genuinely in flight (not paused/waiting) must not get
        # a second, independent invocation spawned alongside it just because
        # a new message arrived — fold the message into the run that's
        # already working instead. create_invocation() already correctly
        # reuses waiting_for_approval/paused runs on its own; this covers
        # the states it doesn't (created/queued/running/waiting_for_tool).
        if active and active["state"] in {
            "created",
            "queued",
            "running",
            "waiting_for_tool",
            "scheduled",
        }:
            kind = "abort" if _looks_like_stop(text) else "steer"
            self.agent.send_signal(active["id"], kind=kind, message=text)
            log.info(
                "Run signal sent: event_id=%s run_id=%s signal=%s",
                event.id,
                active["id"],
                kind,
            )
            return {"route": "run_signal", "run_id": active["id"], "signal": kind}
        run = self.agent.create_invocation(
            envelope=envelope,
            goal=text,
            trigger_kind=TriggerKind.REACTIVE,
            trigger_event_id=event.id,
            initial_message=text,
            metadata={
                "slack": {
                    "workspace_id": event.scope.workspace_id,
                    "channel_id": event.scope.channel_id,
                    "context_key": event.payload.get("context_key"),
                }
            },
        )
        job = self.queue.enqueue(
            organization_id=event.organization_id,
            kind="agent.run",
            payload={"run_id": run["id"]},
            idempotency_key=f"agent.run:{run['id']}",
        )
        log.info(
            "Reactive agent invocation created: event_id=%s run_id=%s channel=%s job_id=%s",
            event.id,
            run["id"],
            event.scope.channel_id,
            job["id"],
        )
        return {"route": "reactive", "run_id": run["id"], "job_id": job["id"]}

    def _store_raw_evidence(self, event: NormalizedEvent) -> dict[str, Any]:
        learn = bool(event.payload.get("learn", event.source != "slack"))
        result = self.ingestion.ingest(
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
                "event_id": event.id,
                "source": event.source,
                "trigger_slug": event.payload.get("trigger_slug"),
                "trigger_id": event.payload.get("trigger_id"),
                "connected_account_id": event.payload.get(
                    "connected_account_id"
                ),
                "raw_data": event.payload.get("raw_data"),
            },
            learn=learn,
            processing_mode="memory" if learn else "reference",
            occurred_at=event.occurred_at,
        )
        self._update_event_graph(event, result["source"])
        self.audit.record(
            event.organization_id,
            "event.routed",
            {
                "event_id": event.id,
                "direct": event.is_direct_agent_request,
                "source_id": result["source"]["id"],
            },
            principal_id=event.actor.id,
        )
        return result["source"]

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
        for entity in self.store.list_entities(event.organization_id):
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
