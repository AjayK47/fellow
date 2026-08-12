"""Permission-rechecked, idempotent Slack delivery and approval cards."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import timedelta
from typing import Any

from toda.domain.models import new_id, utcnow
from toda.harness.router import _ACK_REACTION
from toda.services.scope_resolution import resolve_scope

log = logging.getLogger("toda.delivery")

# Matches the citation IDs the model is asked to cite in factual claims
# (see prompts.py: "Cite source or memory IDs when making factual company
# claims") — new_id()'s format is "{prefix}_{16 hex chars}".
_CITED_ID_PATTERN = re.compile(r"\b(?:src|mem)_[0-9a-f]{16}\b")

# Slack hard-caps a section block's text.text at 3000 characters and
# rejects the entire postMessage call (invalid_blocks) if it's exceeded --
# no partial post, no card at all. A tool call whose arguments embed a
# large text field (a full report/document body, e.g. pdf_create) can
# easily blow past that, so the arguments preview must be truncated with
# real headroom, not dumped verbatim.
_SLACK_SECTION_TEXT_LIMIT = 3000

# How long a "sending" reservation may sit before another attempt may
# reclaim it. Comfortably longer than SlackSender's own 30s HTTP timeout,
# so this only ever fires for a genuinely abandoned claim (a crashed
# process), never for a send that is merely slow.
_STALE_RESERVATION_SECONDS = 300


def _approval_preview_text(prefix: str, arguments: dict[str, Any]) -> str:
    fence = "\n```"
    closing_fence = "```"
    budget = _SLACK_SECTION_TEXT_LIMIT - len(prefix) - len(fence) - len(closing_fence) - 80
    body = str(arguments or {})
    if len(body) <= max(budget, 0):
        return prefix + fence + body + closing_fence
    truncated = body[: max(budget, 0)]
    omitted = len(body) - len(truncated)
    return (
        prefix
        + fence
        + truncated
        + f"\n… truncated ({omitted} more characters)"
        + closing_fence
    )


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

    def _clear_ack(self, task: dict[str, Any], slack: dict[str, Any]) -> None:
        """Remove the "still working on it" reaction router.py added to the
        triggering message the moment this Task started. Best-effort and
        never allowed to fail delivery -- the real answer having posted is
        what matters; a stray hourglass left behind is cosmetic."""
        message_ts = slack.get("message_ts")
        channel_id = slack.get("channel_id")
        if not message_ts or not channel_id:
            return
        try:
            self.slack.unreact(
                channel_id=channel_id,
                timestamp=message_ts,
                name=_ACK_REACTION,
                workspace_id=slack.get("workspace_id"),
            )
        except Exception:
            log.warning("Failed to clear ack reaction for task_id=%s", task.get("id"), exc_info=True)

    def deliver_task(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        if task["state"] != "completed" or not task.get("final_answer"):
            return {
                "task_id": task_id,
                "delivered": False,
                "reason": "task_not_complete",
            }
        if (task.get("metadata") or {}).get("suppress_delivery"):
            # The model already sent its own message directly via
            # slack_send_message (typed_tools.py sets this flag right when
            # that happens, specifically to prevent this). Every OTHER
            # delivery path already honors it (_maybe_notify_failure,
            # deliver_status_update) -- this one didn't, which is exactly
            # how a real production run ended up delivering both the
            # model's direct send AND a redundant auto-delivered final
            # answer as two separate Slack messages for one automation
            # firing.
            return {
                "task_id": task_id,
                "delivered": False,
                "reason": "suppressed",
            }
        envelope = task["envelope"]
        self._recheck(task)
        idempotency_key = f"slack.final:{task_id}"
        reservation = self._reserve(
            task=task, kind="final_answer", idempotency_key=idempotency_key
        )
        if reservation is not None:
            already_sent = reservation.get("status") in {"completed", "dry_run"}
            return {
                "task_id": task_id,
                "delivered": False,
                "reason": "already_delivered" if already_sent else "send_in_progress_elsewhere",
                "delivery": reservation,
            }
        slack = (task.get("metadata") or {}).get("slack") or {}
        destination = envelope["destination"]
        channel_id = slack.get("channel_id") or destination["id"]
        log.info(
            "Delivering final answer: task_id=%s channel=%s",
            task_id,
            channel_id,
        )
        try:
            result = self.slack.send(
                channel_id=channel_id,
                text=task["final_answer"],
                workspace_id=slack.get("workspace_id"),
                organization_id=task["organization_id"],
                idempotency_key=idempotency_key,
            )
        except Exception:
            # Hand the reservation back before propagating, so the job's
            # own retry can actually re-send instead of being locked out by
            # this attempt's abandoned claim.
            self._release_reservation(
                task=task, kind="final_answer", idempotency_key=idempotency_key
            )
            raise
        delivery = self._record(
            task=task,
            kind="final_answer",
            status="completed" if result.get("ok") else "dry_run",
            payload={"provider_result": result, "text": task["final_answer"]},
            idempotency_key=idempotency_key,
        )
        self._clear_ack(task, slack)
        if task["trigger_kind"] == "proactive":
            intervention = {
                "id": new_id("intervention"),
                "organization_id": task["organization_id"],
                "conversation_key": slack.get("context_key")
                or envelope["conversation_id"],
                "task_id": task_id,
                "delivery_id": delivery["id"],
                "destination": destination,
                "created_at": utcnow().isoformat(),
            }
            self.store.insert_intervention(intervention)
            conversation = self.store.conversation_state(
                task["organization_id"], intervention["conversation_key"]
            )
            if conversation:
                conversation["last_agent_intervention_at"] = intervention["created_at"]
                conversation["updated_at"] = intervention["created_at"]
                self.store.upsert_conversation_state(conversation)
        return {"task_id": task_id, "delivered": True, "delivery": delivery}

    def deliver_task_failure(self, task_id: str) -> dict[str, Any]:
        """The failed-task counterpart to final delivery.
        closed (no hang, no partial write — see _run_loop_protected) but
        never tells the human it was working for is functionally identical
        to a hang from their point of view: Slack goes silent forever with
        no error, no retry, nothing. This sends a short best-effort notice
        instead, so "it's stuck" is at minimum "it failed, here's why".
        """
        task = self.store.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        if task["state"] != "failed":
            return {"task_id": task_id, "delivered": False, "reason": "task_not_failed"}
        envelope = task["envelope"]
        self._recheck(task)
        idempotency_key = f"slack.failure:{task_id}"
        reservation = self._reserve(
            task=task, kind="task_failure", idempotency_key=idempotency_key
        )
        if reservation is not None:
            already_sent = reservation.get("status") in {"completed", "dry_run"}
            return {
                "task_id": task_id,
                "delivered": False,
                "reason": "already_delivered" if already_sent else "send_in_progress_elsewhere",
                "delivery": reservation,
            }
        slack = (task.get("metadata") or {}).get("slack") or {}
        destination = envelope["destination"]
        channel_id = slack.get("channel_id") or destination["id"]
        error = str(task.get("error") or "unknown error")[:500]
        text = (
            "Something went wrong and I couldn't finish this one — "
            "no partial action was left running.\n"
            f"```{error}```\n"
            "Feel free to try again or rephrase the request."
        )
        log.info(
            "Delivering task failure: task_id=%s channel=%s",
            task_id,
            channel_id,
        )
        try:
            result = self.slack.send(
                channel_id=channel_id,
                text=text,
                workspace_id=slack.get("workspace_id"),
                organization_id=task["organization_id"],
                idempotency_key=idempotency_key,
            )
        except Exception:
            self._release_reservation(
                task=task, kind="task_failure", idempotency_key=idempotency_key
            )
            raise
        delivery = self._record(
            task=task,
            kind="task_failure",
            status="completed" if result.get("ok") else "dry_run",
            payload={"provider_result": result, "text": text},
            idempotency_key=idempotency_key,
        )
        self._clear_ack(task, slack)
        return {"task_id": task_id, "delivered": True, "delivery": delivery}

    def deliver_status_update(self, task_id: str) -> dict[str, Any]:
        """Edit a single live status message in place as a Task crosses
        toolkit-transition checkpoints (see CompanyAgent._track_checkpoint).
        The first checkpoint that clears the multi-checkpoint bar posts a
        new message; every later checkpoint edits that same message rather
        than posting a new one, so a long multi-toolkit task reads as one
        progress indicator instead of a flood of separate Slack messages.
        """
        task = self.store.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        if (task.get("metadata") or {}).get("suppress_delivery"):
            return {"task_id": task_id, "delivered": False, "reason": "suppressed"}
        self._recheck(task)
        envelope = task["envelope"]
        slack = (task.get("metadata") or {}).get("slack") or {}
        channel_id = slack.get("channel_id") or envelope["destination"]["id"]
        checkpoints = (task.get("metadata") or {}).get("status_checkpoints") or []
        lines = "\n".join(
            f"{'done' if index < len(checkpoints) - 1 else 'working'} — {toolkit}"
            for index, toolkit in enumerate(checkpoints)
        )
        text = f"Working on it...\n{lines}" if lines else "Working on it..."
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
        candidates = self.store.list_harness_records(
            "deliveries", task["organization_id"], where={"task_id": task_id}
        )
        existing = next(
            (item for item in candidates if item.get("kind") == "status_update"), None
        )
        provider_result = (existing or {}).get("provider_result") or {}
        if existing and provider_result.get("channel") and provider_result.get("ts"):
            result = self.slack.update_message(
                channel_id=provider_result["channel"],
                timestamp=provider_result["ts"],
                text=text,
                blocks=blocks,
                workspace_id=slack.get("workspace_id"),
            )
            delivery = dict(existing)
            delivery.update(
                {
                    "status": "completed" if result.get("ok") else "dry_run",
                    "provider_result": result,
                    "text": text,
                    "updated_at": utcnow().isoformat(),
                }
            )
            self.store.upsert_delivery(delivery)
        else:
            result = self.slack.send(
                channel_id=channel_id,
                text=text,
                blocks=blocks,
                workspace_id=slack.get("workspace_id"),
                organization_id=task["organization_id"],
            )
            delivery = self._record(
                task=task,
                kind="status_update",
                status="completed" if result.get("ok") else "dry_run",
                payload={"provider_result": result, "text": text},
                idempotency_key=f"slack.status:{task_id}",
            )
        return {"task_id": task_id, "delivered": True, "delivery": delivery}

    def deliver_approval(self, approval_id: str) -> dict[str, Any]:
        approval = self.store.get_harness_record("approvals", approval_id)
        if not approval:
            raise KeyError(approval_id)
        task = self.store.get_task(approval["task_id"])
        if not task:
            raise KeyError(approval["task_id"])
        self._recheck(task)
        envelope = task["envelope"]
        slack = (task.get("metadata") or {}).get("slack") or {}
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
                    "text": _approval_preview_text(text, preview.get("arguments") or {}),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "action_id": "toda_approve",
                        "value": approval_id,
                    },
                    {
                        "type": "button",
                        "style": "danger",
                        "text": {"type": "plain_text", "text": "Deny"},
                        "action_id": "toda_deny",
                        "value": approval_id,
                    },
                ],
            },
        ]
        result = self.slack.send(
            channel_id=slack.get("channel_id") or envelope["destination"]["id"],
            text=text,
            blocks=blocks,
            workspace_id=slack.get("workspace_id"),
            organization_id=task["organization_id"],
        )
        delivery = self._record(
            task=task,
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

    def mark_approval_decided(
        self, approval_id: str, *, approved: bool, decided_by: str
    ) -> None:
        """Replace the Approve/Deny buttons on the original Slack message
        with the actual decision, once one is made. Best-effort: an
        approval decided from the dashboard (no Slack delivery), a
        dry-run/unconfigured Slack sender, or any lookup failure here must
        never surface as an error to the caller — the approval itself is
        already resolved regardless of whether this cosmetic follow-up
        succeeds.
        """
        try:
            approval = self.store.get_harness_record("approvals", approval_id)
            if not approval:
                return
            # list_harness_records' generic filter only covers real, indexed
            # columns on this table (task_id, status, id, organization_id —
            # not idempotency_key, and "kind" isn't a deliveries column at
            # all despite being in the generic allowlist) — narrow by
            # task_id, then match the exact delivery in Python.
            candidates = self.store.list_harness_records(
                "deliveries",
                approval["organization_id"],
                where={"task_id": approval["task_id"]},
            )
            deliveries = [
                d
                for d in candidates
                if d.get("idempotency_key") == f"slack.approval:{approval_id}"
            ]
            if not deliveries:
                return
            provider_result = deliveries[0].get("provider_result") or {}
            channel_id = provider_result.get("channel")
            ts = provider_result.get("ts")
            if not channel_id or not ts:
                return
            task = self.store.get_task(deliveries[0]["task_id"])
            workspace_id = ((task or {}).get("metadata") or {}).get("slack", {}).get(
                "workspace_id"
            )
            label = "Approved" if approved else "Denied"
            self.slack.update_message(
                channel_id=channel_id,
                timestamp=ts,
                text=f"{label} by {decided_by}",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{label}* by {decided_by}",
                        },
                    }
                ],
                workspace_id=workspace_id,
            )
        except Exception:
            log.warning(
                "Could not update Slack approval message for %s", approval_id,
                exc_info=True,
            )

    def deliver_question(
        self, task_id: str, question: str, *, choices: list[str] | None = None
    ) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        self._recheck(task)
        envelope = task["envelope"]
        slack = (task.get("metadata") or {}).get("slack") or {}
        blocks = self._choice_blocks(task_id, question, choices) if choices else None
        result = self.slack.send(
            channel_id=slack.get("channel_id") or envelope["destination"]["id"],
            text=question,
            blocks=blocks,
            workspace_id=slack.get("workspace_id"),
            organization_id=task["organization_id"],
        )
        delivery = self._record(
            task=task,
            kind="human_question",
            status="completed" if result.get("ok") else "dry_run",
            payload={
                "provider_result": result,
                "question": question,
                "text": question,
            },
            idempotency_key=(
                f"slack.question:{task_id}:"
                f"{hashlib.sha256(question.encode()).hexdigest()[:16]}"
            ),
        )
        return {"task_id": task_id, "delivery": delivery}

    def _choice_blocks(
        self, task_id: str, question: str, choices: list[str]
    ) -> list[dict[str, Any]]:
        # Slack caps interactive element values at 75 chars, far shorter than
        # a choice's text could be — encode a stable index into the value
        # instead and resolve it back against the Task's own stored
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
                            "action_id": "toda_choice",
                            "value": f"{task_id}::{index}",
                        }
                        for index, choice in enumerate(choices)
                    ],
                },
            ]
        section["accessory"] = {
            "type": "static_select",
            "action_id": "toda_choice_select",
            "placeholder": {"type": "plain_text", "text": "Choose an option"},
            "options": [
                {
                    "text": {"type": "plain_text", "text": choice[:75]},
                    "value": f"{task_id}::{index}",
                }
                for index, choice in enumerate(choices)
            ],
        }
        return [section]

    def _recheck(self, task: dict[str, Any]) -> None:
        envelope = task["envelope"]
        decision = self.authz.resolve(
            organization_id=task["organization_id"],
            acting_principal_id=envelope["principal_id"],
            destination=envelope["destination"],
            purpose="agent_delivery",
        )
        if not decision.allowed_scope_ids:
            raise PermissionError("Delivery permission recheck failed closed")
        self._check_disclosure(task)

    def _check_disclosure(self, task: dict[str, Any]) -> None:
        """H-13: verify every piece of evidence the final answer actually
        cites is still releasable to this destination, right now — not just
        that the principal has *some* allowed scope. Recomputed fresh at
        delivery time since permissions can change after a Task starts."""
        answer = task.get("final_answer") or ""
        cited_ids = set(_CITED_ID_PATTERN.findall(answer))
        if not cited_ids:
            return
        envelope = task["envelope"]
        fresh = resolve_scope(
            store=self.store,
            organization_id=task["organization_id"],
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

    def _reserve(
        self, *, task: dict[str, Any], kind: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        """Atomically claim this idempotency key BEFORE the real Slack
        send -- a read-then-send check (look up an existing delivery,
        then send if none found) leaves a real gap: two calls that both
        read "not yet delivered" will both actually post to Slack, and
        the later _record() call (upsert by the same UNIQUE constraint)
        silently collapses them to one row, hiding that it happened.
        Confirmed real: a live automation delivered its reminder text
        twice in the channel with only one delivery row on record.
        Returns None when this call won the reservation (safe to send);
        returns the existing record otherwise (already completed, or a
        concurrent/crashed attempt owns it -- either way this call must
        not send)."""
        now = utcnow()
        placeholder = {
            "id": new_id("delivery"),
            "organization_id": task["organization_id"],
            "task_id": task["id"],
            "kind": kind,
            "status": "sending",
            "idempotency_key": idempotency_key,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        return self.store.reserve_delivery(
            placeholder,
            reclaim_stale_before=(
                now - timedelta(seconds=_STALE_RESERVATION_SECONDS)
            ).isoformat(),
        )

    def _release_reservation(
        self, *, task: dict[str, Any], kind: str, idempotency_key: str
    ) -> None:
        """Mark a reservation failed so a retry can claim it. Without this,
        one transient Slack error would block that message forever."""
        now = utcnow().isoformat()
        self.store.upsert_delivery(
            {
                "id": new_id("delivery"),
                "organization_id": task["organization_id"],
                "task_id": task["id"],
                "kind": kind,
                "status": "failed",
                "idempotency_key": idempotency_key,
                "created_at": now,
                "updated_at": now,
            }
        )

    def _record(
        self,
        *,
        task: dict[str, Any],
        kind: str,
        status: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        now = utcnow().isoformat()
        delivery = {
            "id": new_id("delivery"),
            "organization_id": task["organization_id"],
            "task_id": task["id"],
            "kind": kind,
            "status": status,
            "idempotency_key": idempotency_key,
            **payload,
            "created_at": now,
            "updated_at": now,
        }
        self.store.upsert_delivery(delivery)
        self.audit.record(
            task["organization_id"],
            "task.delivery",
            {
                "task_id": task["id"],
                "delivery_id": delivery["id"],
                "kind": kind,
                "status": status,
            },
            principal_id=task["envelope"]["principal_id"],
            trace_id=task["trace_id"],
        )
        return delivery
