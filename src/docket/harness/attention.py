"""Retrieval-first proactive attention evaluation and deterministic policy."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from docket.domain.models import new_id, utcnow
from docket.harness.models import (
    AttentionDecision,
    AttentionEvaluation,
    InvocationEnvelope,
    NormalizedEvent,
    TriggerKind,
)
from docket.harness.prompts import (
    ATTENTION_SCHEMA,
    ATTENTION_SYSTEM_PROMPT,
)

log = logging.getLogger("docket.attention")

_TERMINAL_RUN_STATES = {"completed", "failed", "cancelled"}


class AttentionPipeline:
    def __init__(
        self,
        *,
        store: Any,
        retrieval: Any,
        model: Any,
        agent: Any,
        queue: Any,
        audit: Any,
        config: Any,
    ) -> None:
        self.store = store
        self.retrieval = retrieval
        self.model = model
        self.agent = agent
        self.queue = queue
        self.audit = audit
        self.config = config

    def process(self, event: NormalizedEvent) -> dict[str, Any]:
        conversation = self._update_conversation_state(event)
        compact = self._build_context(event, conversation)
        response = self.model.generate(
            messages=[
                {"role": "system", "content": ATTENTION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(compact, sort_keys=True),
                },
            ],
            response_schema=ATTENTION_SCHEMA,
            temperature=0.0,
            max_tokens=700,
        )
        try:
            evaluation = AttentionEvaluation(**json.loads(response.content))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            evaluation = AttentionEvaluation(
                decision=AttentionDecision.OBSERVE,
                confidence=0.0,
                reason=f"Attention model output was invalid: {exc}",
            )
        evaluation = self._apply_policy(event, conversation, evaluation)
        record = {
            "id": new_id("attention"),
            "organization_id": event.organization_id,
            "event_id": event.id,
            **evaluation.model_dump(mode="json"),
            "context": compact,
            "model": getattr(self.model, "model", ""),
            "created_at": utcnow().isoformat(),
        }
        self.store.insert_attention_evaluation(record)
        result: dict[str, Any] = {"evaluation": record}

        if evaluation.decision in {
            AttentionDecision.ATTACH_TO_EXISTING_RUN,
            AttentionDecision.ESCALATE_EXISTING_RUN,
        } and conversation.get("active_run_id"):
            run = self.store.get_harness_record(
                "agent_runs", conversation["active_run_id"]
            )
            if run:
                self.agent.runs.add_message(
                    organization_id=event.organization_id,
                    session_id=run["session_id"],
                    run_id=run["id"],
                    role="user",
                    content=event.payload.get("text") or "",
                    metadata={"ambient_event_id": event.id},
                )
                if run["state"] in {"waiting_for_human", "paused", "queued"}:
                    job = self.queue.enqueue(
                        organization_id=event.organization_id,
                        kind="agent.run",
                        payload={"run_id": run["id"]},
                        idempotency_key=f"agent.resume:{run['id']}:{event.id}",
                    )
                    result["job_id"] = job["id"]

        if evaluation.decision in {
            AttentionDecision.WAKE_MAIN_AGENT,
            AttentionDecision.DM_OWNER,
        }:
            run = self._wake(event, evaluation)
            conversation["active_run_id"] = run["id"]
            conversation["updated_at"] = utcnow().isoformat()
            self.store.upsert_conversation_state(conversation)
            job = self.queue.enqueue(
                organization_id=event.organization_id,
                kind="agent.run",
                payload={"run_id": run["id"]},
                idempotency_key=f"agent.run:{run['id']}",
            )
            result.update({"run_id": run["id"], "job_id": job["id"]})

        self.audit.record(
            event.organization_id,
            "attention.evaluated",
            {
                "event_id": event.id,
                "decision": evaluation.decision.value,
                "confidence": evaluation.confidence,
                "run_id": result.get("run_id"),
            },
            principal_id=event.actor.id,
        )
        log.info(
            "Attention decision: event_id=%s decision=%s confidence=%.2f reason=%s run_id=%s channel=%s",
            event.id,
            evaluation.decision.value,
            float(evaluation.confidence),
            evaluation.reason,
            result.get("run_id", ""),
            event.scope.channel_id,
        )
        return result

    def process_event_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.process(NormalizedEvent(**payload))

    def _author_name(self, principal_id: str | None) -> str:
        """Resolve a principal id to a human-readable name for the attention
        classifier. Without this, every participant is an opaque
        person_slack_* string, which makes it look like there's only one
        undifferentiated speaker in a channel — the classifier then has no
        cheap way to notice that a question was addressed by one named human
        to another, not to Docket, and treats topical relevance as if it
        were being spoken to."""
        if not principal_id:
            return "unknown"
        entity = self.store.get_entity(principal_id)
        return (entity or {}).get("name") or principal_id

    def _build_context(
        self, event: NormalizedEvent, conversation: dict[str, Any]
    ) -> dict[str, Any]:
        destination = self._destination(event)
        memories = self.retrieval.search(
            organization_id=event.organization_id,
            query=event.payload.get("text") or " ",
            acting_principal_id=event.actor.id,
            destination=destination,
            purpose="proactive_attention",
            limit=6,
        )
        raw = self.retrieval.search_evidence(
            organization_id=event.organization_id,
            query=event.payload.get("text") or " ",
            acting_principal_id=event.actor.id,
            destination=destination,
            purpose="proactive_attention",
            limit=6,
        )
        active_run = (
            self.store.get_harness_record(
                "agent_runs", conversation.get("active_run_id", "")
            )
            if conversation.get("active_run_id")
            else None
        )
        related_cases: list[dict[str, Any]] = []
        if self.retrieval.reality:
            event_tokens = set(
                (event.payload.get("text") or "").lower().replace("_", " ").split()
            )
            for case in self.retrieval.reality.list_cases(
                event.organization_id,
                statuses={"open", "investigating"},
                scope_ids=memories.get("allowed_scope_ids") or [],
            ):
                case_tokens = set(case.get("correlation_tokens") or [])
                if event_tokens & case_tokens:
                    related_cases.append(
                        {
                            "id": case["id"],
                            "title": case["title"],
                            "status": case["status"],
                            "entity_ids": case.get("entity_ids") or [],
                            "source_ids": case.get("source_ids") or [],
                        }
                    )
        recent_conversation = [
            {**item, "author_name": self._author_name(item.get("author"))}
            for item in (
                self.store.recent_slack_messages(
                    event.organization_id,
                    workspace_id=event.scope.workspace_id or "",
                    channel_id=event.scope.channel_id or "",
                    limit=11,
                )
                if event.source == "slack" and event.scope.channel_id
                else []
            )
            if item.get("message_id") != event.id
        ][-10:]
        return {
            "new_event": {
                "id": event.id,
                "text": event.payload.get("text") or "",
                "author": event.actor.id,
                "author_name": self._author_name(event.actor.id),
                "channel": event.scope.channel_id,
            },
            "recent_conversation": recent_conversation,
            "related_state": {
                "active_run_id": active_run["id"] if active_run else None,
                "active_run_state": active_run.get("state")
                if active_run
                else None,
                "recent_agent_intervention": conversation.get(
                    "last_agent_intervention_at"
                ),
                "last_human_acknowledgement_at": conversation.get(
                    "last_human_acknowledgement_at"
                ),
                "proactivity_enabled": bool(
                    event.payload.get("proactivity_enabled")
                ),
            },
            "related_memories": memories["results"],
            "related_facts": memories.get("reality_facts") or [],
            "related_cases": related_cases[:5],
            "judgment_guidance": (
                self.retrieval.reality.judgment_guidance(
                    event.organization_id,
                    event.actor.id,
                    limit=5,
                    query=event.payload.get("text") or "",
                )
                if self.retrieval.reality
                else []
            ),
            "related_messages": raw["results"],
            "permission_decision_ids": [
                memories["permission_decision_id"],
                raw["permission_decision_id"],
            ],
        }

    def _apply_policy(
        self,
        event: NormalizedEvent,
        conversation: dict[str, Any],
        evaluation: AttentionEvaluation,
    ) -> AttentionEvaluation:
        if event.is_direct_agent_request:
            evaluation.decision = AttentionDecision.IGNORE
            evaluation.reason = "Direct requests bypass the attention pipeline."
            return evaluation
        active_run_id = conversation.get("active_run_id")
        if active_run_id:
            active_run = self.store.get_harness_record("agent_runs", active_run_id)
            if not active_run or active_run.get("state") in _TERMINAL_RUN_STATES:
                # Stale pointer: that run already finished (or was never
                # cleared after finishing — nothing currently clears
                # active_run_id on completion). Attaching to it would
                # silently strand this message with no further processing
                # ever happening, so clear it and let this event get a
                # fresh decision instead of a dead reference.
                conversation["active_run_id"] = None
                conversation["updated_at"] = utcnow().isoformat()
                self.store.upsert_conversation_state(conversation)
                active_run_id = None
                if evaluation.decision in {
                    AttentionDecision.ATTACH_TO_EXISTING_RUN,
                    AttentionDecision.ESCALATE_EXISTING_RUN,
                }:
                    evaluation.decision = AttentionDecision.WAKE_MAIN_AGENT
                    evaluation.reason = (
                        "Referenced run already finished; treating as a new request."
                    )
        if active_run_id and evaluation.decision in {
            AttentionDecision.WAKE_MAIN_AGENT,
            AttentionDecision.DM_OWNER,
        }:
            evaluation.decision = AttentionDecision.ATTACH_TO_EXISTING_RUN
            evaluation.existing_run_id = active_run_id
            evaluation.reason = "An active run already owns this conversation."
            return evaluation
        since = (
            utcnow()
            - timedelta(seconds=self.config.proactive_cooldown_seconds)
        ).isoformat()
        recent = self.store.recent_intervention(
            event.organization_id,
            event.payload.get("context_key") or event.payload["conversation_key"],
            since=since,
        )
        if recent and evaluation.decision in {
            AttentionDecision.WAKE_MAIN_AGENT,
            AttentionDecision.DM_OWNER,
        }:
            evaluation.decision = AttentionDecision.OBSERVE
            evaluation.reason = "Suppressed by proactive intervention cooldown."
            return evaluation
        if (
            evaluation.confidence < self.config.attention_minimum_confidence
            and evaluation.decision
            in {
                AttentionDecision.WAKE_MAIN_AGENT,
                AttentionDecision.DM_OWNER,
                AttentionDecision.ESCALATE_EXISTING_RUN,
            }
        ):
            evaluation.decision = AttentionDecision.OBSERVE
            evaluation.reason = "Attention confidence is below policy threshold."
            return evaluation
        if (
            not event.payload.get("proactivity_enabled")
            and evaluation.decision
            in {
                AttentionDecision.WAKE_MAIN_AGENT,
                AttentionDecision.DM_OWNER,
            }
        ):
            evaluation.decision = AttentionDecision.ADD_TO_DIGEST
            evaluation.reason = "Proactive delivery is disabled for this channel."
        return evaluation

    def _wake(
        self,
        event: NormalizedEvent,
        evaluation: AttentionEvaluation,
    ) -> dict[str, Any]:
        destination = self._destination(event)
        authz = self.retrieval.authorize(
            event.organization_id,
            event.actor.id,
            destination,
            "proactive_investigation",
        )
        envelope = InvocationEnvelope(
            organization_id=event.organization_id,
            principal_id=event.actor.id,
            application_id=f"{event.source}_docket",
            conversation_id=event.payload["conversation_key"],
            destination=destination,
            purpose="proactive_investigation",
            permission_decision_id=authz.id,
            allowed_scope_ids=authz.allowed_scope_ids,
        )
        goal = evaluation.suggested_goal or (
            "Investigate this company event and intervene only if useful: "
            + (event.payload.get("text") or "")
        )
        return self.agent.create_invocation(
            envelope=envelope,
            goal=goal,
            trigger_kind=TriggerKind.PROACTIVE,
            trigger_event_id=event.id,
            initial_message=(
                f"Proactive trigger from {event.source}:\n"
                + (event.payload.get("text") or "")
                + "\nAttention reason: "
                + evaluation.reason
            ),
            metadata={
                "attention_decision": evaluation.model_dump(mode="json"),
                "slack": {
                    "workspace_id": event.scope.workspace_id,
                    "channel_id": event.scope.channel_id,
                    "context_key": event.payload.get("context_key"),
                    "actor_profile": event.payload.get("actor_profile") or {
                        "principal_id": event.actor.id,
                        "source_user_id": event.actor.source_id,
                        "display_name": event.actor.id,
                    },
                },
            },
        )

    def _update_conversation_state(self, event: NormalizedEvent) -> dict[str, Any]:
        key = event.payload["conversation_key"]
        key = event.payload.get("context_key") or key
        conversation = self.store.conversation_state(event.organization_id, key)
        if not conversation:
            conversation = {
                "id": new_id("conversation_state"),
                "organization_id": event.organization_id,
                "conversation_key": key,
                "workspace_id": event.scope.workspace_id,
                "channel_id": event.scope.channel_id,
                "participants": [],
                "recent_messages": [],
                "active_run_id": None,
                "last_agent_intervention_at": None,
                "last_human_acknowledgement_at": None,
                "possible_topics": [],
                "status": "active",
            }
        participants = set(conversation.get("participants") or [])
        participants.add(event.actor.id)
        conversation["participants"] = sorted(participants)
        messages = list(conversation.get("recent_messages") or [])
        messages.append(
            {
                "event_id": event.id,
                "author": event.actor.id,
                "text": event.payload.get("text") or "",
                "at": event.occurred_at.isoformat(),
            }
        )
        conversation["recent_messages"] = messages[-30:]
        lower = (event.payload.get("text") or "").lower()
        if any(word in lower for word in ("thanks", "ack", "investigating")):
            conversation["last_human_acknowledgement_at"] = (
                event.occurred_at.isoformat()
            )
        conversation["updated_at"] = utcnow().isoformat()
        self.store.upsert_conversation_state(conversation)
        return conversation

    @staticmethod
    def _destination(event: NormalizedEvent) -> dict[str, Any]:
        configured = event.payload.get("destination")
        if isinstance(configured, dict) and configured.get("type"):
            return {
                "type": configured["type"],
                "id": str(configured.get("id") or ""),
                "is_private": bool(configured.get("is_private", True)),
                "audience_principal_ids": list(
                    configured.get("audience_principal_ids") or []
                ),
            }
        private = event.scope.visibility != "organization_public"
        if event.source != "slack":
            return {
                "type": "internal_agent_session",
                "id": event.scope.channel_id or event.source,
                "is_private": private,
            }
        return {
            "type": "dm"
            if event.type == "slack.dm"
            else "private_channel"
            if private
            else "slack_channel",
            "id": event.scope.channel_id or "",
            "is_private": private,
        }
