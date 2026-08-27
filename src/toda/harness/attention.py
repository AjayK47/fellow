"""Retrieval-first proactive attention evaluation and deterministic policy."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from toda.domain.models import new_id, utcnow
from toda.harness.models import (
    AttentionDecision,
    AttentionEvaluation,
    NormalizedEvent,
)
from toda.harness.prompts import (
    ATTENTION_SCHEMA,
    ATTENTION_SYSTEM_PROMPT,
)

log = logging.getLogger("toda.attention")

_TERMINAL_TASK_STATES = {"completed", "failed", "cancelled"}


class AttentionPipeline:
    def __init__(
        self,
        *,
        store: Any,
        retrieval: Any,
        model: Any,
        queue: Any,
        audit: Any,
        config: Any,
        sessions: Any,
    ) -> None:
        self.store = store
        self.retrieval = retrieval
        self.model = model
        self.queue = queue
        self.audit = audit
        self.config = config
        self.sessions = sessions

    def _active_root_task(self, conversation: dict[str, Any]) -> dict[str, Any] | None:
        """Find the active root Task for a Session.
        find_root_task_for_session() already excludes terminal states, so a
        A missing result means no live work owns this conversation right now."""
        session_id = conversation.get("active_session_id")
        if not session_id:
            return None
        return self.store.find_root_task_for_session(session_id)

    def process(self, event: NormalizedEvent) -> dict[str, Any]:
        conversation = self._update_conversation_state(event)
        matched_roles = self._match_roles(event)
        compact = self._build_context(event, conversation, matched_roles)
        with self.store.telemetry.span(
            "attention.classify",
            component="attention",
            organization_id=event.organization_id,
            event_id=event.id,
            input={"matched_roles": matched_roles},
        ) as span:
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
            span.output({"finish_reason": response.finish_reason})
        try:
            evaluation = AttentionEvaluation(**json.loads(response.content))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            evaluation = AttentionEvaluation(
                decision=AttentionDecision.OBSERVE,
                confidence=0.0,
                reason=f"Attention model output was invalid: {exc}",
            )
        evaluation = self._apply_policy(event, conversation, evaluation, matched_roles)
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

        # Session/Task path (plan §7.2): ambient events wake the Session
        # Manager rather than creating a run directly. sessions.wake()
        # already handles "attach to whatever's active vs start fresh"
        # (via the Session's own revision/dispatch machinery), so
        # ATTACH_TO_EXISTING_TASK/ESCALATE_EXISTING_TASK and WAKE_MAIN_AGENT/
        # DM_OWNER all collapse onto the same call -- the distinction
        # between them only still matters for the attention model's own
        # prompt context and the cooldown policy in _apply_policy.
        if evaluation.decision in {
            AttentionDecision.ATTACH_TO_EXISTING_TASK,
            AttentionDecision.ESCALATE_EXISTING_TASK,
            AttentionDecision.WAKE_MAIN_AGENT,
            AttentionDecision.DM_OWNER,
        }:
            wake = self._wake_session(event, evaluation)
            conversation["active_session_id"] = wake["session_id"]
            conversation["updated_at"] = utcnow().isoformat()
            self.store.upsert_conversation_state(conversation)
            job = self.queue.enqueue(
                organization_id=event.organization_id,
                kind="session.dispatch",
                payload={"session_id": wake["session_id"]},
                idempotency_key=f"session.dispatch:{wake['session_id']}:{wake['revision']}",
            )
            result.update({"session_id": wake["session_id"], "job_id": job["id"]})

        self.audit.record(
            event.organization_id,
            "attention.evaluated",
            {
                "event_id": event.id,
                "decision": evaluation.decision.value,
                "confidence": evaluation.confidence,
                "session_id": result.get("session_id"),
            },
            principal_id=event.actor.id,
        )
        log.info(
            "Attention decision: event_id=%s decision=%s confidence=%.2f reason=%s session_id=%s channel=%s",
            event.id,
            evaluation.decision.value,
            float(evaluation.confidence),
            evaluation.reason,
            result.get("session_id", ""),
            event.scope.channel_id,
        )
        return result

    def process_event_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.process(NormalizedEvent(**payload))

    _ROLE_STOP_WORDS = {
        "a", "an", "and", "before", "for", "in", "is", "it", "of", "on",
        "or", "the", "to", "when", "you", "see", "something", "that",
        "reads", "like",
    }

    def _match_roles(self, event: NormalizedEvent) -> list[dict[str, Any]]:
        """Durable, semantic "wake up and run X whenever you see Y" triggers.

        Evaluated the same way the rest of attention works today: from the
        raw event text via token overlap against the role's free-text
        trigger_description, not bound to any channel/scope, reusing the
        related_cases correlation-token pattern below for consistency."""
        event_tokens = set(
            (event.payload.get("text") or "").lower().replace("_", " ").split()
        )
        matched: list[dict[str, Any]] = []
        for role in self.store.list_harness_records(
            "roles", event.organization_id, where={"status": "active"}
        ):
            trigger_tokens = {
                tok
                for tok in (role.get("trigger_description") or "")
                .lower()
                .replace("_", " ")
                .split()
                if tok not in self._ROLE_STOP_WORDS and len(tok) > 1
            }
            if len(event_tokens & trigger_tokens) >= 2:
                matched.append(
                    {
                        "id": role["id"],
                        "name": role.get("name"),
                        "goal": role.get("goal"),
                        "skill_ids": role.get("skill_ids") or [],
                        "trigger_description": role.get("trigger_description"),
                    }
                )
        return matched

    def _author_name(self, principal_id: str | None) -> str:
        """Resolve a principal id to a human-readable name for the attention
        classifier. Without this, every participant is an opaque
        person_slack_* string, which makes it look like there's only one
        undifferentiated speaker in a channel — the classifier then has no
        cheap way to notice that a question was addressed by one named human
        to another, not to Toda, and treats topical relevance as if it
        were being spoken to."""
        if not principal_id:
            return "unknown"
        entity = self.store.get_entity(principal_id)
        return (entity or {}).get("name") or principal_id

    def _build_context(
        self,
        event: NormalizedEvent,
        conversation: dict[str, Any],
        matched_roles: list[dict[str, Any]] | None = None,
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
        active_task = self._active_root_task(conversation)
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
                "active_task_id": active_task["id"] if active_task else None,
                "active_task_state": active_task.get("state")
                if active_task
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
            "watched_roles": matched_roles or [],
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
        matched_roles: list[dict[str, Any]] | None = None,
    ) -> AttentionEvaluation:
        if event.is_direct_agent_request:
            evaluation.decision = AttentionDecision.IGNORE
            evaluation.reason = "Direct requests bypass the attention pipeline."
            return evaluation
        if matched_roles:
            # A standing role trigger always wakes the main agent -- the whole
            # point of subscribing to one is to not get silently muted by the
            # ordinary confidence floor or proactive cooldown that guards
            # everyday ambient chatter.
            role = matched_roles[0]
            evaluation.decision = AttentionDecision.WAKE_MAIN_AGENT
            evaluation.matched_role_id = role["id"]
            evaluation.suggested_goal = role.get("goal") or (
                f"Act on the '{role.get('name')}' role: "
                + (event.payload.get("text") or "")
            )
            evaluation.reason = f"Matched active role trigger '{role.get('name')}'."
            return evaluation
        active_session_id = conversation.get("active_session_id")
        if active_session_id:
            active_task = self._active_root_task(conversation)
            if not active_task:
                # Stale pointer: find_root_task_for_session() already
                # excludes terminal states, so a None result means that
                # Session's root Task finished (or there never was one --
                # nothing currently clears active_session_id on
                # completion). Attaching to it would silently strand this
                # message with no further processing ever happening, so
                # clear it and let this event get a fresh decision instead
                # of a dead reference.
                conversation["active_session_id"] = None
                conversation["updated_at"] = utcnow().isoformat()
                self.store.upsert_conversation_state(conversation)
                active_session_id = None
                if evaluation.decision in {
                    AttentionDecision.ATTACH_TO_EXISTING_TASK,
                    AttentionDecision.ESCALATE_EXISTING_TASK,
                }:
                    evaluation.decision = AttentionDecision.WAKE_MAIN_AGENT
                    evaluation.reason = (
                        "Referenced Task already finished; treating this as a new request."
                    )
        if active_session_id and evaluation.decision in {
            AttentionDecision.WAKE_MAIN_AGENT,
            AttentionDecision.DM_OWNER,
        }:
            evaluation.decision = AttentionDecision.ATTACH_TO_EXISTING_TASK
            evaluation.existing_task_id = active_session_id
            evaluation.reason = "An active Task already owns this conversation."
            return evaluation
        since = (
            utcnow()
            - timedelta(seconds=self.config.worker.proactive_cooldown_seconds)
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
            evaluation.confidence < self.config.worker.attention_minimum_confidence
            and evaluation.decision
            in {
                AttentionDecision.WAKE_MAIN_AGENT,
                AttentionDecision.DM_OWNER,
                AttentionDecision.ESCALATE_EXISTING_TASK,
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

    def _wake_session(
        self,
        event: NormalizedEvent,
        evaluation: AttentionEvaluation,
    ) -> dict[str, Any]:
        """Session/Task path (plan §7.2/§7.3): an ambient event judged
        useful wakes the Session for this conversation exactly like a
        direct message would (EventRouter._route_reactive), just with a
        proactive-investigation framing and purpose instead of a literal
        user request. sessions.wake() handles authorization itself at
        dispatch time (plan §7.4 -- every wake re-authorizes fresh), so
        this no longer needs its own authz.resolve() call because the Session
        create_invocation()-based _wake() did."""
        destination = self._destination(event)
        metadata: dict[str, Any] = {
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
            "attention_decision": evaluation.model_dump(mode="json"),
        }
        if evaluation.matched_role_id:
            role = self.store.get_harness_record("roles", evaluation.matched_role_id)
            if role:
                metadata["role_skill_ids"] = role.get("skill_ids") or []
        content = (
            f"Proactive trigger from {event.source}:\n"
            + (event.payload.get("text") or "")
            + "\nAttention reason: "
            + evaluation.reason
        )
        return self.sessions.wake(
            organization_id=event.organization_id,
            # Ambient Slack events carry a per-event evidence key as
            # conversation_key and the stable Slack conversation identity as
            # context_key.  Session ownership must use the latter so all
            # turns in one channel/thread share one Manager.
            conversation_key=(
                event.payload.get("context_key")
                or event.payload["conversation_key"]
            ),
            destination=destination,
            role="event",
            content=content,
            principal_id=event.actor.id,
            metadata=metadata,
            message_metadata={
                "trigger_event_id": event.id,
                "ambient_event_id": event.id,
                "attention_decision": evaluation.model_dump(mode="json"),
            },
            trigger_kind="proactive",
            purpose="proactive_attention",
            # An ambient platform event: the actor spoke in the room, which
            # the platform authorized before we saw it.
            may_join=True,
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
                "active_session_id": None,
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
