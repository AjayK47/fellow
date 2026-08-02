"""Permission-rechecked, idempotent Slack delivery and approval cards."""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from docket.domain.models import new_id, utcnow
from docket.services.scope_resolution import resolve_scope

log = logging.getLogger("docket.delivery")

# Matches the citation IDs the model is asked to cite in factual claims
# (see prompts.py: "Cite source or memory IDs when making factual company
# claims") — new_id()'s format is "{prefix}_{16 hex chars}".
_CITED_ID_PATTERN = re.compile(r"\b(?:src|mem)_[0-9a-f]{16}\b")


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
        log.info(
            "Delivering final answer: run_id=%s channel=%s",
            run_id,
            channel_id,
        )
        result = self.slack.send(
            channel_id=channel_id,
            text=run["final_answer"],
        )
        delivery = self._record(
            run=run,
            kind="final_answer",
            status="completed" if result.get("ok") else "dry_run",
            payload={"provider_result": result, "text": run["final_answer"]},
            idempotency_key=f"slack.final:{run_id}",
        )
        if run["trigger_kind"] == "proactive":
            intervention = {
                "id": new_id("intervention"),
                "organization_id": run["organization_id"],
                "conversation_key": slack.get("context_key")
                or envelope["conversation_id"],
                "run_id": run_id,
                "delivery_id": delivery["id"],
                "destination": destination,
                "created_at": utcnow().isoformat(),
            }
            self.store.insert_intervention(intervention)
            conversation = self.store.conversation_state(
                run["organization_id"], intervention["conversation_key"]
            )
            if conversation:
                conversation["last_agent_intervention_at"] = intervention["created_at"]
                conversation["updated_at"] = intervention["created_at"]
                self.store.upsert_conversation_state(conversation)
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
                        "action_id": "docket_approve",
                        "value": approval_id,
                    },
                    {
                        "type": "button",
                        "style": "danger",
                        "text": {"type": "plain_text", "text": "Deny"},
                        "action_id": "docket_deny",
                        "value": approval_id,
                    },
                ],
            },
        ]
        result = self.slack.send(
            channel_id=slack.get("channel_id") or envelope["destination"]["id"],
            text=text,
            blocks=blocks,
        )
        delivery = self._record(
            run=run,
            kind="approval_request",
            status="completed" if result.get("ok") else "dry_run",
            payload={
                "approval_id": approval_id,
                "provider_result": result,
                "text": text,
            },
            idempotency_key=f"slack.approval:{approval_id}",
        )
        return {"approval_id": approval_id, "delivery": delivery}

    def deliver_question(
        self, run_id: str, question: str, *, choices: list[str] | None = None
    ) -> dict[str, Any]:
        run = self.store.get_harness_record("agent_runs", run_id)
        if not run:
            raise KeyError(run_id)
        self._recheck(run)
        envelope = run["envelope"]
        slack = (run.get("metadata") or {}).get("slack") or {}
        blocks = self._choice_blocks(run_id, question, choices) if choices else None
        result = self.slack.send(
            channel_id=slack.get("channel_id") or envelope["destination"]["id"],
            text=question,
            blocks=blocks,
        )
        delivery = self._record(
            run=run,
            kind="human_question",
            status="completed" if result.get("ok") else "dry_run",
            payload={
                "provider_result": result,
                "question": question,
                "text": question,
            },
            idempotency_key=(
                f"slack.question:{run_id}:"
                f"{hashlib.sha256(question.encode()).hexdigest()[:16]}"
            ),
        )
        return {"run_id": run_id, "delivery": delivery}

    def _choice_blocks(
        self, run_id: str, question: str, choices: list[str]
    ) -> list[dict[str, Any]]:
        # Slack caps interactive element values at 75 chars, far shorter than
        # a choice's text could be — encode a stable index into the value
        # instead and resolve it back against the run's own stored
        # pending_question_choices (see SlackGateway._resolve_interaction),
        # rather than round-tripping the choice text through Slack itself.
        section = {"type": "section", "text": {"type": "mrkdwn", "text": question}}
        if len(choices) <= 5 and all(len(c) <= 75 for c in choices):
            return [
                section,
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": choice[:75]},
                            "action_id": "docket_choice",
                            "value": f"{run_id}::{index}",
                        }
                        for index, choice in enumerate(choices)
                    ],
                },
            ]
        section["accessory"] = {
            "type": "static_select",
            "action_id": "docket_choice_select",
            "placeholder": {"type": "plain_text", "text": "Choose an option"},
            "options": [
                {
                    "text": {"type": "plain_text", "text": choice[:75]},
                    "value": f"{run_id}::{index}",
                }
                for index, choice in enumerate(choices)
            ],
        }
        return [section]

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
        self._check_disclosure(run)

    def _check_disclosure(self, run: dict[str, Any]) -> None:
        """H-13: verify every piece of evidence the final answer actually
        cites is still releasable to this destination, right now — not just
        that the principal has *some* allowed scope. Recomputed fresh at
        delivery time since permissions can change after a run starts."""
        answer = run.get("final_answer") or ""
        cited_ids = set(_CITED_ID_PATTERN.findall(answer))
        if not cited_ids:
            return
        envelope = run["envelope"]
        fresh = resolve_scope(
            store=self.store,
            organization_id=run["organization_id"],
            principal_id=envelope["principal_id"],
            destination=envelope["destination"],
        )
        for cited_id in cited_ids:
            record = (
                self.store.get_source(cited_id)
                if cited_id.startswith("src_")
                else self.store.get_memory(cited_id)
            )
            if record is None:
                continue
            scope_id = record.get("scope_id")
            if scope_id and scope_id not in fresh.read_scope_ids:
                raise PermissionError(
                    f"Delivery blocked: cited evidence {cited_id} is outside "
                    "the current destination's allowed scopes"
                )

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
