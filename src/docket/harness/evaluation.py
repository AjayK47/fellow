"""Harness evaluation metrics and explicit human feedback capture."""

from __future__ import annotations

from collections import Counter
from typing import Any

from docket.domain.models import new_id, utcnow


class HarnessEvaluationService:
    def __init__(
        self, *, store: Any, audit: Any, reality: Any | None = None
    ) -> None:
        self.store = store
        self.audit = audit
        self.reality = reality

    def record_feedback(
        self,
        *,
        organization_id: str,
        feedback_type: str,
        principal_id: str,
        run_id: str | None = None,
        attention_evaluation_id: str | None = None,
        comment: str | None = None,
    ) -> dict[str, Any]:
        allowed = {
            "helpful",
            "not_helpful",
            "correction",
            "dismissed",
            "acknowledged",
            "missed_critical_event",
            "action_correct",
            "action_incorrect",
        }
        if feedback_type not in allowed:
            raise ValueError(f"unsupported feedback type: {feedback_type}")
        item = {
            "id": new_id("feedback"),
            "organization_id": organization_id,
            "run_id": run_id,
            "attention_evaluation_id": attention_evaluation_id,
            "feedback_type": feedback_type,
            "principal_id": principal_id,
            "comment": comment,
            "created_at": utcnow().isoformat(),
        }
        with self.store.cursor() as cur:
            cur.execute(
                """INSERT INTO agent_feedback
                (id, organization_id, run_id, attention_evaluation_id,
                 feedback_type, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    item["id"],
                    organization_id,
                    run_id,
                    attention_evaluation_id,
                    feedback_type,
                    __import__("json").dumps(item),
                    item["created_at"],
                ),
            )
        self.audit.record(
            organization_id,
            "agent.feedback",
            item,
            principal_id=principal_id,
        )
        if self.reality:
            self.reality.record_judgment(
                organization_id=organization_id,
                judgment_type="agent_feedback",
                principal_id=principal_id,
                subject={
                    "run_id": run_id,
                    "attention_evaluation_id": attention_evaluation_id,
                },
                decision=feedback_type,
                reason=comment,
            )
        return item

    def metrics(self, organization_id: str) -> dict[str, Any]:
        attentions = self.store.list_harness_records(
            "attention_evaluations", organization_id
        )
        runs = self.store.list_harness_records("agent_runs", organization_id)
        calls = self.store.list_harness_records(
            "harness_tool_calls", organization_id
        )
        feedback = self.store.list_harness_records(
            "agent_feedback", organization_id
        )
        decisions = Counter(item["decision"] for item in attentions)
        run_states = Counter(item["state"] for item in runs)
        run_kinds = Counter(item["trigger_kind"] for item in runs)
        tool_states = Counter(item["status"] for item in calls)
        feedback_counts = Counter(item["feedback_type"] for item in feedback)
        wake_count = decisions.get("WAKE_MAIN_AGENT", 0) + decisions.get(
            "DM_OWNER", 0
        )
        attaches = decisions.get("ATTACH_TO_EXISTING_RUN", 0) + decisions.get(
            "ESCALATE_EXISTING_RUN", 0
        )
        labeled_positive = feedback_counts.get("helpful", 0) + feedback_counts.get(
            "acknowledged", 0
        )
        labeled_negative = feedback_counts.get(
            "not_helpful", 0
        ) + feedback_counts.get("dismissed", 0)
        labeled_total = labeled_positive + labeled_negative
        completed = run_states.get("completed", 0)
        terminal = completed + run_states.get("failed", 0) + run_states.get(
            "cancelled", 0
        )
        return {
            "organization_id": organization_id,
            "attention": {
                "evaluations": len(attentions),
                "decisions": dict(decisions),
                "wake_count": wake_count,
                "attach_instead_of_duplicate_count": attaches,
                "labeled_wake_precision": (
                    labeled_positive / labeled_total if labeled_total else None
                ),
                "missed_critical_events": feedback_counts.get(
                    "missed_critical_event", 0
                ),
            },
            "runs": {
                "count": len(runs),
                "states": dict(run_states),
                "trigger_kinds": dict(run_kinds),
                "terminal_task_completion_rate": (
                    completed / terminal if terminal else None
                ),
                "total_model_tokens": sum(
                    int((run.get("usage") or {}).get("total_tokens", 0))
                    for run in runs
                ),
            },
            "tools": {
                "count": len(calls),
                "states": dict(tool_states),
                "approval_compliance": all(
                    call.get("risk") != "write"
                    or bool(call.get("approval_id"))
                    for call in calls
                ),
            },
            "memory": {
                "sources": len(self.store.list_sources(organization_id)),
                "current_memories": len(
                    self.store.list_memories(organization_id, latest_only=True)
                ),
                "total_memories": len(
                    self.store.list_memories(organization_id, latest_only=False)
                ),
            },
            "feedback": dict(feedback_counts),
        }
