"""Permission-rechecked, idempotent Slack delivery and approval cards."""

from __future__ import annotations

import hashlib
from typing import Any

from company_brain.domain.models import new_id, utcnow


class DeliveryService:
    def __init__(
        self,
        *,
        store: Any,
        authz: Any,
        slack_sender: Any,
        audit: Any,
    ) -> None:
        self.store = store
        self.authz = authz
        self.slack = slack_sender
        self.audit = audit

    def deliver_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_harness_record("agent_runs", run_id)
        if not run:
            raise KeyError(run_id)
        if run["state"] != "completed" or not run.get("final_answer"):
            return {
                "run_id": run_id,
                "delivered": False,
                "reason": "run_not_complete",
            }
        envelope = run["envelope"]
        self._recheck(run)
        slack = (run.get("metadata") or {}).get("slack") or {}
        destination = envelope["destination"]
        channel_id = slack.get("channel_id") or destination["id"]
        thread_id = slack.get("thread_id")
        result = self.slack.send(
            channel_id=channel_id,
            thread_id=thread_id,
            text=run["final_answer"],
        )
        delivery = self._record(
            run=run,
            kind="final_answer",
            status="completed" if result.get("ok") else "dry_run",
            payload={"provider_result": result},
            idempotency_key=f"slack.final:{run_id}",
        )
        if run["trigger_kind"] == "proactive":
            intervention = {
                "id": new_id("intervention"),
                "organization_id": run["organization_id"],
                "conversation_key": envelope["conversation_id"],
                "run_id": run_id,
                "delivery_id": delivery["id"],
                "destination": destination,
                "created_at": utcnow().isoformat(),
            }
            self.store.insert_intervention(intervention)
            thread = self.store.thread_state(
                run["organization_id"], envelope["conversation_id"]
            )
            if thread:
                thread["last_agent_intervention_at"] = intervention["created_at"]
                thread["updated_at"] = intervention["created_at"]
                self.store.upsert_thread_state(thread)
        return {"run_id": run_id, "delivered": True, "delivery": delivery}

    def deliver_approval(self, approval_id: str) -> dict[str, Any]:
        approval = self.store.get_harness_record("approvals", approval_id)
        if not approval:
            raise KeyError(approval_id)
        run = self.store.get_harness_record("agent_runs", approval["run_id"])
        if not run:
            raise KeyError(approval["run_id"])
        self._recheck(run)
        envelope = run["envelope"]
        slack = (run.get("metadata") or {}).get("slack") or {}
        preview = approval["preview"]
        text = (
            f"Approval required: {preview['description']}\n"
            f"Action: `{preview['action']}`"
        )
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": text
                    + "\n```"
                    + str(preview.get("arguments") or {})
                    + "```",
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "action_id": "company_brain_approve",
                        "value": approval_id,
                    },
                    {
                        "type": "button",
                        "style": "danger",
                        "text": {"type": "plain_text", "text": "Deny"},
                        "action_id": "company_brain_deny",
                        "value": approval_id,
                    },
                ],
            },
        ]
        result = self.slack.send(
            channel_id=slack.get("channel_id") or envelope["destination"]["id"],
            thread_id=slack.get("thread_id"),
            text=text,
            blocks=blocks,
        )
        delivery = self._record(
            run=run,
            kind="approval_request",
            status="completed" if result.get("ok") else "dry_run",
            payload={"approval_id": approval_id, "provider_result": result},
            idempotency_key=f"slack.approval:{approval_id}",
        )
        return {"approval_id": approval_id, "delivery": delivery}

    def deliver_question(self, run_id: str, question: str) -> dict[str, Any]:
        run = self.store.get_harness_record("agent_runs", run_id)
        if not run:
            raise KeyError(run_id)
        self._recheck(run)
        envelope = run["envelope"]
        slack = (run.get("metadata") or {}).get("slack") or {}
        result = self.slack.send(
            channel_id=slack.get("channel_id") or envelope["destination"]["id"],
            thread_id=slack.get("thread_id"),
            text=question,
        )
        delivery = self._record(
            run=run,
            kind="human_question",
            status="completed" if result.get("ok") else "dry_run",
            payload={"provider_result": result, "question": question},
            idempotency_key=(
                f"slack.question:{run_id}:"
                f"{hashlib.sha256(question.encode()).hexdigest()[:16]}"
            ),
        )
        return {"run_id": run_id, "delivery": delivery}

    def _recheck(self, run: dict[str, Any]) -> None:
        envelope = run["envelope"]
        decision = self.authz.resolve(
            organization_id=run["organization_id"],
            acting_principal_id=envelope["principal_id"],
            destination=envelope["destination"],
            purpose="agent_delivery",
        )
        if not decision.allowed_scope_ids:
            raise PermissionError("Delivery permission recheck failed closed")

    def _record(
        self,
        *,
        run: dict[str, Any],
        kind: str,
        status: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        now = utcnow().isoformat()
        delivery = {
            "id": new_id("delivery"),
            "organization_id": run["organization_id"],
            "run_id": run["id"],
            "kind": kind,
            "status": status,
            "idempotency_key": idempotency_key,
            **payload,
            "created_at": now,
            "updated_at": now,
        }
        self.store.upsert_delivery(delivery)
        self.audit.record(
            run["organization_id"],
            "agent.delivery",
            {
                "run_id": run["id"],
                "delivery_id": delivery["id"],
                "kind": kind,
                "status": status,
            },
            principal_id=run["envelope"]["principal_id"],
            trace_id=run["trace_id"],
        )
        return delivery
