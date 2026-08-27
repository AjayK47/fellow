"""Permission-rechecked, idempotent Slack delivery and approval cards."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from toda.domain.models import new_id, utcnow
from toda.harness.router import _ACK_REACTION
from toda.harness.slack_markdown import to_slack_mrkdwn
from toda.services.scope_resolution import resolve_scope

log = logging.getLogger("toda.delivery")

# Defense in depth for old Tasks that still contain opaque evidence IDs.
# Current prompts ask for human-readable citations, but delivery still checks
# any legacy IDs it encounters before letting content cross a destination.
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


# One line of output, but the budget has to cover the model's own thinking
# too: reasoning cannot be switched off on every provider (ai-studio rejects
# effort "none"), so a tight cap gets spent deliberating and the sentence
# comes back truncated mid-word. The text is clipped to 300 characters after
# the fact, so the real cost stays bounded by what the model actually thinks.
_ACK_MAX_TOKENS = 1_500

_ACK_SYSTEM_PROMPT = """You are Toda, a colleague at this company, replying \
in Slack the instant a request arrives — before you have started work.

Write ONE short sentence acknowledging what was asked, in your own voice.

- Name the specific thing, so it is clear you actually read it.
- Say what you are about to do, not what you have found. You have done
  nothing yet and know nothing yet.
- Never promise a time, never invent findings, never ask a question.
- No greeting, no sign-off, no emoji, no preamble. Just the sentence.

Examples of the register:
"Let me pull the Japanese rail and GDP figures and put a report together."
"Digging into what's blocking the Acme renewal now."
"I'll check the deploy history and see what changed last week."
"""



_APPROVAL_MAX_TOKENS = 1_500

_APPROVAL_SYSTEM_PROMPT = """You are Toda, asking a colleague in Slack to \
approve one action before you take it.

Write ONE short sentence saying plainly what you are about to do and to
what, so they can decide without knowing anything about how you work.

- Name the real subject: the file, the document, the channel, the record.
- No tool names, no function names, no JSON, no field names, no IDs.
- Present tense, first person, what you will do: "Post the finished
  report to #social", "Create a PDF of the Q3 revenue summary".
- No greeting, no please, no explanation of why, no question mark.
- If you genuinely cannot tell what the action does, say so in plain
  words rather than guessing.
"""


@dataclass(frozen=True)
class DeliveryOwner:
    """What a delivery belongs to.

    A Task for the five task-driven sends, a Session for a conversation
    reply. Both need the identical atomic reservation, so the reservation
    helpers take this instead of a Task -- previously they took a Task
    outright, which is why conversation delivery could not use them and
    quietly kept the double-post race everywhere else had closed.
    """

    organization_id: str
    task_id: str | None = None
    session_id: str | None = None
    message_id: str | None = None
    principal_id: str | None = None

    @classmethod
    def for_task(cls, task: dict[str, Any]) -> "DeliveryOwner":
        return cls(
            organization_id=task["organization_id"],
            task_id=task["id"],
            session_id=task.get("session_id"),
            principal_id=(task.get("envelope") or {}).get("principal_id"),
        )

    @classmethod
    def for_session_message(
        cls, session: dict[str, Any], message_id: str
    ) -> "DeliveryOwner":
        return cls(
            organization_id=session["organization_id"],
            session_id=session["id"],
            message_id=message_id,
            principal_id=session.get("last_actor_principal_id"),
        )


class DeliveryService:
    def __init__(
        self,
        *,
        store: Any,
        authz: Any,
        slack_sender: Any,
        audit: Any,
        model: Any | None = None,
    ) -> None:
        self.store = store
        self.authz = authz
        self.slack = slack_sender
        self.audit = audit
        # Deliberately the small auxiliary model, not the main one. The
        # acknowledgement exists so a person is not left in silence while
        # the real model spins up; paying main-model latency to produce it
        # would defeat its whole purpose.
        self.model = model

    def deliver_conversation(self, session_id: str, message_id: str) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if not session:
            raise KeyError(session_id)
        message = next(
            (
                item
                for item in self.store.list_session_messages(session_id, limit=200)
                if item.get("id") == message_id and item.get("role") == "assistant"
            ),
            None,
        )
        if not message:
            raise KeyError(message_id)
        destination = session.get("destination") or {}
        principal_id = session.get("last_actor_principal_id") or "unknown"
        self.authz.resolve(
            organization_id=session["organization_id"],
            acting_principal_id=principal_id,
            destination=destination,
            purpose="conversation_delivery",
        )
        metadata = session.get("metadata") or {}
        slack = metadata.get("slack") or {}
        idempotency_key = f"slack.conversation:{message_id}"
        owner = DeliveryOwner.for_session_message(session, message_id)
        # The same atomic claim every task-driven send uses. This path used
        # to look up an existing delivery and then send, which is precisely
        # the gap reserve_delivery() exists to close -- and it is the most
        # frequent send in the system, so it was the likeliest place for the
        # confirmed double-post to recur.
        reservation = self._reserve(
            owner=owner, kind="conversation", idempotency_key=idempotency_key
        )
        if reservation is not None:
            already_sent = reservation.get("status") in {"completed", "dry_run"}
            return {
                "session_id": session_id,
                "delivered": False,
                "reason": "already_delivered"
                if already_sent
                else "send_in_progress_elsewhere",
                "delivery": reservation,
            }
        try:
            result = self.slack.send(
                channel_id=slack.get("channel_id") or destination.get("id") or "",
                text=to_slack_mrkdwn(str(message.get("content") or "")),
                workspace_id=slack.get("workspace_id"),
                organization_id=session["organization_id"],
                idempotency_key=idempotency_key,
            )
        except Exception:
            # Hand the claim back so the job's own retry can re-send rather
            # than being locked out by this attempt's abandoned reservation.
            self._release_reservation(
                owner=owner, kind="conversation", idempotency_key=idempotency_key
            )
            raise
        delivery = self._record(
            owner=owner,
            kind="conversation",
            status="completed" if result.get("ok") else "dry_run",
            payload={"provider_result": result},
            idempotency_key=idempotency_key,
        )
        # A quick reply is Toda speaking too. This path never cleared the
        # reaction, so every greeting left an hourglass sitting on the
        # user's message forever.
        self._clear_ack(session_id, slack)
        self.audit.record(
            session["organization_id"],
            "conversation.delivered",
            {"session_id": session_id, "message_id": message_id},
            principal_id=principal_id,
        )
        return {"session_id": session_id, "delivered": True, "delivery": delivery}


    def _clear_ack(self, task_or_id: Any, slack: dict[str, Any]) -> None:
        """Remove the hourglass router.py put on the triggering message.

        Cleared the moment Toda actually says something -- the reaction
        exists to cover the gap between "your message arrived" and "Toda
        has spoken", so once a real message is posted it is redundant.
        For a Task that means its acknowledgement, not its final answer:
        leaving it up through a multi-minute run after Toda has already
        replied just reads as stuck.

        Best-effort and never allowed to fail delivery -- the message
        having posted is what matters; a stray hourglass is cosmetic.
        Unreacting something already gone is expected, not an error.
        """
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
            log.warning(
                "Failed to clear ack reaction for %s",
                task_or_id if isinstance(task_or_id, str) else (task_or_id or {}).get("id"),
                exc_info=True,
            )

    def deliver_task(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        self.store.telemetry.record(
            "delivery.send", component="delivery", phase="started",
            input={"task_id": task_id, "state": task.get("state")},
            task_id=task_id,
        )
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
            #
            # Record the skip rather than writing nothing. An absent
            # delivery row is indistinguishable from "we never got round to
            # it", which is exactly how these Tasks came to match
            # list_completed_tasks_missing_delivery() forever -- and since
            # that sweep is ORDER BY completed_at LIMIT 100, a hundred
            # accumulated suppressed Tasks permanently filled its window and
            # a genuinely undelivered newer answer was never even examined.
            self._record(
                owner=DeliveryOwner.for_task(task),
                kind="final_answer",
                status="suppressed",
                payload={"reason": "model_sent_its_own_message"},
                idempotency_key=f"slack.final:{task_id}",
            )
            return {
                "task_id": task_id,
                "delivered": False,
                "reason": "suppressed",
            }
        envelope = task["envelope"]
        self._recheck(task)
        idempotency_key = f"slack.final:{task_id}"
        reservation = self._reserve(
            owner=DeliveryOwner.for_task(task), kind="final_answer", idempotency_key=idempotency_key
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
                # The model writes Markdown; Slack speaks mrkdwn. Blocks here
                # have always declared "mrkdwn" while carrying raw Markdown,
                # so "### Heading" and "**bold**" rendered literally.
                text=to_slack_mrkdwn(task["final_answer"]),
                workspace_id=slack.get("workspace_id"),
                organization_id=task["organization_id"],
                idempotency_key=idempotency_key,
            )
        except Exception:
            # Hand the reservation back before propagating, so the job's
            # own retry can actually re-send instead of being locked out by
            # this attempt's abandoned claim.
            self._release_reservation(
                owner=DeliveryOwner.for_task(task), kind="final_answer", idempotency_key=idempotency_key
            )
            raise
        delivery = self._record(
            owner=DeliveryOwner.for_task(task),
            kind="final_answer",
            status="completed" if result.get("ok") else "dry_run",
            payload={"provider_result": result, "text": task["final_answer"]},
            idempotency_key=idempotency_key,
        )
        # Safety net: normally the acknowledgement already cleared this.
        # A run whose acknowledgement never posted (model failure,
        # suppressed delivery) would otherwise keep its hourglass
        # forever. Unreacting an absent reaction is a no-op.
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
        result = {"task_id": task_id, "delivered": True, "delivery": delivery}
        self.store.telemetry.record(
            "delivery.send", component="delivery", phase="completed", output=result,
            task_id=task_id,
        )
        return result

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
            owner=DeliveryOwner.for_task(task), kind="task_failure", idempotency_key=idempotency_key
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
                owner=DeliveryOwner.for_task(task), kind="task_failure", idempotency_key=idempotency_key
            )
            raise
        delivery = self._record(
            owner=DeliveryOwner.for_task(task),
            kind="task_failure",
            status="completed" if result.get("ok") else "dry_run",
            payload={"provider_result": result, "text": text},
            idempotency_key=idempotency_key,
        )
        # Safety net: normally the acknowledgement already cleared this.
        # A run whose acknowledgement never posted (model failure,
        # suppressed delivery) would otherwise keep its hourglass
        # forever. Unreacting an absent reaction is a no-op.
        self._clear_ack(task, slack)
        return {"task_id": task_id, "delivered": True, "delivery": delivery}

    def deliver_acknowledgement(self, task_id: str) -> dict[str, Any]:
        """Say one personalized line the moment a Task is created.

        A request used to sit in silence for the whole first reasoning turn
        — retrieval, a full main-model call, then tool calls — which on a
        real request is upwards of ten seconds with nothing to show that
        anything was heard.

        Written by the small auxiliary model rather than a fixed string so
        it names the actual request; a canned "working on it" reads like an
        autoresponder and tells the person nothing about whether Toda
        understood them. It runs on the infrastructure lane, so it posts
        while the agent lane is still on its first turn.

        Best-effort throughout: this is a courtesy, and no failure to
        produce it may affect the Task. It is sent exactly once per Task.
        """
        task = self.store.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        metadata = task.get("metadata") or {}
        if metadata.get("suppress_delivery"):
            return {"task_id": task_id, "delivered": False, "reason": "suppressed"}
        # A child Task is private specialist evidence; only the root
        # conversation ever speaks (mirrors _enqueue_post_run_jobs).
        if task.get("parent_task_id"):
            return {"task_id": task_id, "delivered": False, "reason": "child_task"}
        if self.model is None:
            return {"task_id": task_id, "delivered": False, "reason": "no_model"}

        self._recheck(task)
        text = self._acknowledgement_text(task)
        if not text:
            return {"task_id": task_id, "delivered": False, "reason": "no_text"}

        envelope = task["envelope"]
        slack = metadata.get("slack") or {}
        channel_id = slack.get("channel_id") or envelope["destination"]["id"]
        result = self.slack.send(
            channel_id=channel_id,
            text=text,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
            workspace_id=slack.get("workspace_id"),
            organization_id=task["organization_id"],
        )
        # Toda has now spoken, so the "heard you, not spoken yet" hourglass
        # has done its job.
        self._clear_ack(task, slack)
        delivery = self._record(
            owner=DeliveryOwner.for_task(task),
            kind="acknowledgement",
            status="completed" if result.get("ok") else "dry_run",
            payload={"provider_result": result, "text": text},
            # One per Task, whatever else re-enqueues: the queue's own
            # idempotency is what makes "exactly once" true here.
            idempotency_key=f"slack.acknowledgement:{task_id}",
        )
        return {"task_id": task_id, "delivered": True, "delivery": delivery}

    def _acknowledgement_text(self, task: dict[str, Any]) -> str:
        """One sentence in Toda's voice, or "" if the model gives nothing.

        Never raises: an acknowledgement is a courtesy, and a model hiccup
        must not fail the Task that is about to do the real work.
        """
        goal = str(task.get("goal") or "").strip()
        if not goal:
            return ""
        try:
            response = self.model.generate(
                messages=[
                    {"role": "system", "content": _ACK_SYSTEM_PROMPT},
                    {"role": "user", "content": goal},
                ],
                temperature=0.4,
                max_tokens=_ACK_MAX_TOKENS,
                # No deliberation for one sentence. A reasoning model given
                # this budget spends all of it thinking and returns nothing,
                # and the whole point is to land before the main model has
                # finished starting.
                reasoning=False,
            )
        except Exception:
            log.warning(
                "Acknowledgement model call failed for task_id=%s",
                task.get("id"), exc_info=True,
            )
            return ""
        # One line: a model that ignores the instruction and writes a
        # paragraph must not turn a courtesy into a wall of text.
        # Strip whitespace again after the quotes: a model that wraps its
        # sentence in quotes leaves the padding behind otherwise.
        line = " ".join(str(response.content or "").split()).strip('"').strip()
        return line[:300]

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
                owner=DeliveryOwner.for_task(task),
                kind="status_update",
                status="completed" if result.get("ok") else "dry_run",
                payload={"provider_result": result, "text": text},
                idempotency_key=f"slack.status:{task_id}",
            )
        return {"task_id": task_id, "delivered": True, "delivery": delivery}

    def _approval_sentence(self, task: dict[str, Any], preview: dict[str, Any]) -> str:
        """Plain language for the approval card, or "" to fall back.

        The card used to read "Approval required: <tool description>" over
        "Action: `tool_name`" and then the whole arguments dict in a code
        fence. Nobody approving anything needs a function name and a JSON
        blob -- they need to know what is about to happen, in their own
        terms. An approval that cannot be read is not a control.

        Never raises: a courtesy that breaks the approval it introduces is
        worse than a plainly-worded fallback.
        """
        if self.model is None:
            return ""
        request = json.dumps(
            {
                "what_the_person_asked_for": str(task.get("goal") or "")[:400],
                "action": preview.get("action"),
                "action_description": preview.get("description"),
                "details": preview.get("arguments") or {},
            },
            default=str,
        )[:4_000]
        try:
            response = self.model.generate(
                messages=[
                    {"role": "system", "content": _APPROVAL_SYSTEM_PROMPT},
                    {"role": "user", "content": request},
                ],
                temperature=0.2,
                max_tokens=_APPROVAL_MAX_TOKENS,
                reasoning=False,
            )
        except Exception:
            log.warning(
                "Approval summary model call failed for task_id=%s",
                task.get("id"), exc_info=True,
            )
            return ""
        line = " ".join(str(response.content or "").split()).strip('"').strip()
        return line[:300]

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
        sentence = self._approval_sentence(task, preview)
        text = (
            f"*{sentence}*"
            if sentence
            else f"Approval required: {preview['description']}\n"
                 f"Action: `{preview['action']}`"
        )
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
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
            owner=DeliveryOwner.for_task(task),
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
            owner=DeliveryOwner.for_task(task),
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

    def _base_delivery(
        self, owner: DeliveryOwner, *, kind: str, status: str
    ) -> dict[str, Any]:
        now = utcnow().isoformat()
        return {
            "id": new_id("delivery"),
            "organization_id": owner.organization_id,
            "task_id": owner.task_id,
            "session_id": owner.session_id,
            "message_id": owner.message_id,
            "kind": kind,
            "status": status,
            "created_at": now,
            "updated_at": now,
        }

    def _reserve(
        self, *, owner: DeliveryOwner, kind: str, idempotency_key: str
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
        not send).

        Takes a DeliveryOwner rather than a Task because conversation
        replies -- the most frequent send in the system -- have no Task to
        key off, and that is exactly why deliver_conversation was still on
        the unsafe read-then-send path this method exists to replace.
        """
        now = utcnow()
        placeholder = {
            **self._base_delivery(owner, kind=kind, status="sending"),
            "idempotency_key": idempotency_key,
        }
        return self.store.reserve_delivery(
            placeholder,
            reclaim_stale_before=(
                now - timedelta(seconds=_STALE_RESERVATION_SECONDS)
            ).isoformat(),
        )

    def _release_reservation(
        self, *, owner: DeliveryOwner, kind: str, idempotency_key: str
    ) -> None:
        """Mark a reservation failed so a retry can claim it. Without this,
        one transient Slack error would block that message forever."""
        self.store.upsert_delivery(
            {
                **self._base_delivery(owner, kind=kind, status="failed"),
                "idempotency_key": idempotency_key,
            }
        )

    def _record(
        self,
        *,
        owner: DeliveryOwner,
        kind: str,
        status: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        delivery = {
            **self._base_delivery(owner, kind=kind, status=status),
            "idempotency_key": idempotency_key,
            **payload,
        }
        self.store.upsert_delivery(delivery)
        self.audit.record(
            owner.organization_id,
            "task.delivery",
            {
                "task_id": owner.task_id,
                "session_id": owner.session_id,
                "delivery_id": delivery["id"],
                "kind": kind,
                "status": status,
            },
            principal_id=owner.principal_id,
        )
        return delivery
