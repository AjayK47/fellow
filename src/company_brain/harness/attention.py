"""Retrieval-first proactive attention evaluation and deterministic policy."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from company_brain.domain.models import new_id, utcnow
from company_brain.harness.models import (
    AttentionDecision,
    AttentionEvaluation,
    InvocationEnvelope,
    NormalizedEvent,
    TriggerKind,
)
from company_brain.harness.prompts import (
    ATTENTION_SCHEMA,
    ATTENTION_SYSTEM_PROMPT,
)


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
        thread = self._update_thread_state(event)
        compact = self._build_context(event, thread)
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
        evaluation = self._apply_policy(event, thread, evaluation)
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
        } and thread.get("active_run_id"):
            run = self.store.get_harness_record(
                "agent_runs", thread["active_run_id"]
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
            thread["active_run_id"] = run["id"]
            thread["updated_at"] = utcnow().isoformat()
            self.store.upsert_thread_state(thread)
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
        return result

    def process_event_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.process(NormalizedEvent(**payload))

    def _build_context(
        self, event: NormalizedEvent, thread: dict[str, Any]
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
                "agent_runs", thread.get("active_run_id", "")
            )
            if thread.get("active_run_id")
            else None
        )
        return {
            "new_event": {
                "id": event.id,
                "text": event.payload.get("text") or "",
                "author": event.actor.id,
                "channel": event.scope.channel_id,
                "thread": event.scope.thread_id,
            },
            "recent_thread": [
                {
                    "event_id": item.get("event_id"),
                    "author": item.get("author"),
                    "text": item.get("text"),
                    "at": item.get("at"),
                }
                for item in (thread.get("recent_messages") or [])[-8:]
            ],
            "related_state": {
                "active_run_id": active_run["id"] if active_run else None,
                "active_run_state": active_run.get("state")
                if active_run
                else None,
                "recent_agent_intervention": thread.get(
                    "last_agent_intervention_at"
                ),
                "last_human_acknowledgement_at": thread.get(
                    "last_human_acknowledgement_at"
                ),
                "proactivity_enabled": bool(
                    event.payload.get("proactivity_enabled")
                ),
            },
            "related_memories": memories["results"],
            "related_messages": raw["results"],
            "permission_decision_ids": [
                memories["permission_decision_id"],
                raw["permission_decision_id"],
            ],
        }

    def _apply_policy(
        self,
        event: NormalizedEvent,
        thread: dict[str, Any],
        evaluation: AttentionEvaluation,
    ) -> AttentionEvaluation:
        if event.is_direct_agent_request:
            evaluation.decision = AttentionDecision.IGNORE
            evaluation.reason = "Direct requests bypass the attention pipeline."
            return evaluation
        if thread.get("active_run_id") and evaluation.decision in {
            AttentionDecision.WAKE_MAIN_AGENT,
            AttentionDecision.DM_OWNER,
        }:
            evaluation.decision = AttentionDecision.ATTACH_TO_EXISTING_RUN
            evaluation.existing_run_id = thread["active_run_id"]
            evaluation.reason = "An active run already owns this thread."
            return evaluation
        since = (
            utcnow()
            - timedelta(seconds=self.config.proactive_cooldown_seconds)
        ).isoformat()
        recent = self.store.recent_intervention(
            event.organization_id,
            event.payload["conversation_key"],
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
        self, event: NormalizedEvent, evaluation: AttentionEvaluation
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
            application_id="slack_company_brain",
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
                "Proactive trigger from an ambient Slack message:\n"
                + (event.payload.get("text") or "")
                + "\nAttention reason: "
                + evaluation.reason
            ),
            metadata={"attention_decision": evaluation.model_dump(mode="json")},
        )

    def _update_thread_state(self, event: NormalizedEvent) -> dict[str, Any]:
        key = event.payload["conversation_key"]
        thread = self.store.thread_state(event.organization_id, key)
        if not thread:
            thread = {
                "id": new_id("thread"),
                "organization_id": event.organization_id,
                "conversation_key": key,
                "workspace_id": event.scope.workspace_id,
                "channel_id": event.scope.channel_id,
                "thread_id": event.scope.thread_id,
                "participants": [],
                "recent_messages": [],
                "active_run_id": None,
                "last_agent_intervention_at": None,
                "last_human_acknowledgement_at": None,
                "possible_topics": [],
                "status": "active",
            }
        participants = set(thread.get("participants") or [])
        participants.add(event.actor.id)
        thread["participants"] = sorted(participants)
        messages = list(thread.get("recent_messages") or [])
        messages.append(
            {
                "event_id": event.id,
                "author": event.actor.id,
                "text": event.payload.get("text") or "",
                "at": event.occurred_at.isoformat(),
            }
        )
        thread["recent_messages"] = messages[-30:]
        lower = (event.payload.get("text") or "").lower()
        if any(word in lower for word in ("thanks", "ack", "investigating")):
            thread["last_human_acknowledgement_at"] = event.occurred_at.isoformat()
        thread["updated_at"] = utcnow().isoformat()
        self.store.upsert_thread_state(thread)
        return thread

    @staticmethod
    def _destination(event: NormalizedEvent) -> dict[str, Any]:
        private = event.scope.visibility != "organization_public"
        return {
            "type": "dm"
            if event.type == "slack.dm"
            else "private_channel"
            if private
            else "slack_channel",
            "id": event.scope.channel_id or "",
            "is_private": private,
        }
