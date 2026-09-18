"""Cache-friendly prompt construction with explicit trust boundaries.

Prompt prose lives in ``harness/prompting/templates``; this module owns only
assembly: message order, trust-boundary framing, and optional run blocks.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from toda.harness.prompting.catalog import load, render


CORE_SYSTEM_PROMPT = load("core")
STEER_GUIDANCE = load("steer_guidance")
AUTOMATION_TRIGGER_GUIDANCE = load("automation_trigger_guidance")


def render_voice(persona_name: str = "Toda") -> str:
    """Shared voice block, prepended to user-facing prompts only."""
    return render("voice", persona_name=persona_name)


def is_user_facing_run(run: dict[str, Any]) -> bool:
    """Roots speak to people; child specialists report to their parent."""
    if run.get("parent_task_id"):
        return False
    worker_type = run.get("worker_type") or (run.get("metadata") or {}).get("worker_type")
    return worker_type not in {"general", "pi"}


def _speaker_label(item: dict[str, Any]) -> str:
    """Display name for a user turn, or "" when the author is unknown.

    Prefers the display name a caller resolved (EventRouter passes the Slack
    profile's) and falls back to the principal id, which is at least stable
    and distinguishable. Messages written before attribution existed simply
    render unlabelled, exactly as they did before.
    """
    metadata = item.get("metadata") or {}
    return str(metadata.get("author_name") or metadata.get("principal_id") or "")


def build_agent_messages(
    *,
    run: dict[str, Any],
    context: dict[str, Any],
    history: list[dict[str, Any]],
    conversation_summary: str = "",
    slack_conversation: list[dict[str, Any]] | None = None,
    active_skills: list[dict[str, Any]] | None = None,
    available_skills: list[dict[str, Any]] | None = None,
    persona_name: str = "Toda",
) -> list[dict[str, Any]]:
    envelope = run["envelope"]
    invocation = {
        "task_id": run["id"],
        "goal": run["goal"],
        "trigger_kind": run["trigger_kind"],
        "principal_id": envelope["principal_id"],
        "destination": envelope["destination"],
        "purpose": envelope["purpose"],
        "allowed_scope_ids": envelope["allowed_scope_ids"],
        # Only the *limits* live here. The counters and the clock moved to a
        # trailing block -- see _runtime_state_block below for why.
        "budgets": {
            "max_iterations": run["max_iterations"],
            "max_tool_calls": run["max_tool_calls"],
        },
    }
    # "session_manager" is the root Task's OWN worker_type (set by
    # session_manager.py/child_tasks.py on every root Task's metadata), not
    # a child specialist assignment -- only "general"/"pi" child Tasks get
    # the specialist framing below (must_not_address_user,
    # external_writes_forbidden, <specialist_assignment>). Treating
    # "session_manager" as truthy here was a real, confirmed production bug:
    # it told the root Session Manager itself "you are a specialist, do not
    # address Slack directly, return your result to the parent Session
    # Manager" -- which is exactly backwards for the one Task that's
    # actually supposed to talk to Slack directly and own the conversation.
    # That's what produced replies like "...should be handled by the parent
    # Session Manager" and "No action taken in this worker: outbound Slack
    # delivery is restricted here" from the root Task on a scheduled
    # automation firing.
    raw_worker_type = (run.get("metadata") or {}).get("worker_type")
    worker = raw_worker_type if raw_worker_type in {"general", "pi"} else None
    slack_metadata = (run.get("metadata") or {}).get("slack") or {}
    actor_profile = slack_metadata.get("actor_profile") or {}
    now_utc = datetime.now(timezone.utc)
    timezone_name = actor_profile.get("timezone") or "UTC"
    try:
        now_local = now_utc.astimezone(ZoneInfo(timezone_name))
    except Exception:
        timezone_name = "UTC"
        now_local = now_utc
    invocation["actor"] = {
        "principal_id": envelope["principal_id"],
        "source_user_id": actor_profile.get("source_user_id"),
        "display_name": actor_profile.get("display_name")
        or actor_profile.get("real_name")
        or envelope["principal_id"],
        "real_name": actor_profile.get("real_name"),
    }
    runtime_state: dict[str, Any] = {
        "used_iterations": run.get("iteration_count", 0),
        "used_tool_calls": run.get("tool_call_count", 0),
    }
    runtime_state["current_time"] = {
        "utc": now_utc.isoformat(),
        "local": now_local.isoformat(),
        "timezone": timezone_name,
    }
    if worker:
        invocation["worker"] = {
            "specialist_name": (run.get("metadata") or {}).get("specialist_name")
            or "General Specialist",
            "specialist_instructions": (run.get("metadata") or {}).get(
                "specialist_instructions"
            ),
            "role": (run.get("metadata") or {}).get("worker_role"),
            "depth": (run.get("metadata") or {}).get("worker_depth"),
            "allowed_tools": (run.get("metadata") or {}).get("allowed_tools"),
            "output_contract": (run.get("metadata") or {}).get(
                "output_contract"
            ),
            "must_return_to_parent": True,
            "must_not_address_user": True,
            "external_writes_forbidden": not (run.get("metadata") or {}).get(
                "worker_has_write_tools", False
            ),
        }
    messages: list[dict[str, Any]] = []
    if is_user_facing_run(run):
        messages.append({"role": "system", "content": render_voice(persona_name)})
    messages.append({"role": "system", "content": CORE_SYSTEM_PROMPT})
    messages.append(
        {
            "role": "system",
            "content": "<trusted_invocation>\n"
            + json.dumps(invocation, sort_keys=True)
            + "\n</trusted_invocation>",
        },
    )
    attachments = list((run.get("metadata") or {}).get("attachments") or [])
    if worker and attachments:
        messages.append(
            {
                "role": "system",
                "content": (
                    "<trusted_attachments>\n"
                    "These references were permission-checked by the harness. "
                    "Use the matching read tool and load only what you need; "
                    "the references do not contain the underlying content.\n"
                    + json.dumps(attachments, sort_keys=True)
                    + "\n</trusted_attachments>"
                ),
            }
        )
    if worker:
        specialist_name = invocation["worker"]["specialist_name"]
        specialist_instructions = invocation["worker"].get(
            "specialist_instructions"
        )
        messages.append(
            {
                "role": "system",
                "content": (
                    "<specialist_assignment>\n"
                    + render(
                        "specialist_assignment", specialist_name=specialist_name
                    )
                    + (
                        "\nSpecialist guidance: " + specialist_instructions
                        if specialist_instructions
                        else ""
                    )
                    + "\n</specialist_assignment>"
                ),
            }
        )
    if slack_conversation:
        messages.append(
            {
                "role": "system",
                "content": "<slack_conversation>\n"
                + json.dumps(slack_conversation, sort_keys=True)
                + "\n</slack_conversation>",
            }
        )
    if available_skills:
        messages.append(
            {
                "role": "system",
                "content": (
                    "<available_skills>\n"
                    + load("available_skills_header")
                    + "\n"
                    + json.dumps(available_skills, sort_keys=True)
                    + "\n</available_skills>"
                ),
            }
        )
    if active_skills:
        messages.append(
            {
                "role": "system",
                "content": (
                    "<active_skills>\n"
                    + load("active_skills_header")
                    + "\n"
                    + json.dumps(active_skills, sort_keys=True)
                    + "\n</active_skills>"
                ),
            }
        )
    organization_memory = str(context.get("organization_memory") or "").strip()
    if organization_memory:
        messages.append(
            {
                "role": "system",
                "content": "<organization_memory>\n"
                + organization_memory[:4_000]
                + "\n</organization_memory>",
            }
        )
    messages.append(
        {
            "role": "system",
            "content": "<company_context>\n"
            + json.dumps(context, sort_keys=True)
            + "\n</company_context>",
        }
    )
    if run["trigger_kind"] == "automation":
        messages.append({"role": "system", "content": AUTOMATION_TRIGGER_GUIDANCE})
    if any((item.get("metadata") or {}).get("steered") for item in history):
        messages.append({"role": "system", "content": STEER_GUIDANCE})
    # LAST system block, deliberately. Prefix caching matches an exact token
    # prefix, so anything that changes between iterations invalidates
    # everything after it. These three fields -- the iteration counter, the
    # tool-call counter and the clock -- change on literally every call, and
    # they used to sit in <trusted_invocation> at message #2. That put a
    # mutating value immediately after the system prompt, so the cacheable
    # prefix was the system prompt and nothing else: measured at ~5k of a
    # ~43k-token request, or 11.6%. Every skill body, the organization
    # memory and the whole company_context block were re-billed uncached on
    # every iteration of every Task.
    #
    # Kept as a separate labelled block rather than merged into the
    # invocation so it is obvious to the next person that its position is
    # load-bearing.
    messages.append(
        {
            "role": "system",
            "content": "<runtime_state>\n"
            + json.dumps(runtime_state, sort_keys=True)
            + "\n</runtime_state>",
        }
    )
    if conversation_summary:
        from toda.harness.history_compaction import SUMMARY_PREFIX

        messages.append(
            {
                "role": "system",
                "content": "<conversation_summary>\n"
                + SUMMARY_PREFIX
                + "\n\n"
                + conversation_summary
                + "\n</conversation_summary>",
            }
        )
    for item in history:
        role = item.get("role")
        if role == "event":
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "<untrusted_event>\n"
                        + load("untrusted_event_header")
                        + "\n"
                        + (item.get("content") or "")
                        + "\n</untrusted_event>"
                    ),
                }
            )
            continue
        if role not in {"user", "assistant", "tool"}:
            continue
        content = item.get("content") or ""
        if role == "user":
            # A room's Session is shared by everyone in it, so a user turn is
            # only unambiguous with its speaker attached. Without this,
            # "same thing here" from a second person reads as the original
            # requester talking to themselves, and the model cannot tell one
            # request from two. Rendered at prompt time only -- the stored
            # message content stays clean, so a Task's goal is never polluted
            # with a speaker label.
            speaker = _speaker_label(item)
            if speaker:
                content = f"{speaker}: {content}"
        message: dict[str, Any] = {
            "role": role,
            "content": content,
        }
        if role == "assistant" and item.get("tool_calls"):
            message["tool_calls"] = item["tool_calls"]
        if role == "tool":
            message["tool_call_id"] = item.get("tool_call_id")
            if item.get("name"):
                message["name"] = item["name"]
        messages.append(message)
    return messages


ATTENTION_SYSTEM_PROMPT = load("attention_classifier")


ATTENTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "signal_type": {"type": "string"},
        "novelty": {"type": "string"},
        "urgency": {"type": "string"},
        "decision": {
            "type": "string",
            "enum": [
                "IGNORE",
                "OBSERVE",
                "UPDATE_WORKING_STATE",
                "ATTACH_TO_EXISTING_TASK",
                "ADD_TO_DIGEST",
                "WAKE_MAIN_AGENT",
                "DM_OWNER",
                "ESCALATE_EXISTING_TASK",
            ],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
        "suggested_goal": {"type": ["string", "null"]},
        "related_entity_ids": {"type": "array", "items": {"type": "string"}},
        "existing_task_id": {"type": ["string", "null"]},
        "matched_role_id": {"type": ["string", "null"]},
    },
    "required": [
        "signal_type",
        "novelty",
        "urgency",
        "decision",
        "confidence",
        "reason",
        "suggested_goal",
        "related_entity_ids",
        "existing_task_id",
        "matched_role_id",
    ],
    "additionalProperties": False,
}


# Tools whose output Toda itself produced, and which therefore need no
# untrusted framing. Everything else is framed.
#
# This list is deliberately the TRUSTED half. It began as the untrusted
# half -- {"web_search", "web_fetch"} -- which meant a connector action
# returning a CRM note, a browser tool returning page text, an MCP server's
# response, and extracted document bodies all reached the model as ordinary
# trusted output. Those carry text written by people outside the company
# just as surely as a web page does, and prompt injection does not care
# which tool fetched it.
#
# Naming the trusted half instead makes a newly registered tool untrusted
# until someone decides otherwise: over-framing costs a few tokens and a
# slightly more cautious model, under-framing costs an injected instruction
# executed with the company's credentials.
_TRUSTED_TOOL_NAMES = frozenset(
    {
        # Toda's own memory and evidence, already permission-filtered. The
        # content originally came from company sources, but it has been
        # through ingestion, redaction and scope authorization to get here.
        "memory_search",
        "memory_read_source",
        "memory_query_facts",
        "case_list",
        "case_update",
        "judgment_guidance",
        "owner_resolve",
        "procedure_find",
        "artifact_list",
        "artifact_read",
        "artifact_create",
        # Toda's own control surfaces: plans, scratchpads, Tasks, skills,
        # workspace, schedule, arithmetic. Output here is Toda's own state.
        "calculate",
        "task_delegate",
        "task_manage",
        "plan_create",
        "plan_get",
        "plan_update",
        "request_approval",
        "ask_user",
        "scratchpad_read",
        "scratchpad_update",
        "session_search",
        "skill_validate",
        "skill_read",
        "skill_list",
        "skill_propose_create",
        "skill_propose_update",
        "task_delegate_parallel",
        # Outbound Slack tools return an acknowledgement of what Toda itself
        # just sent (ok / ts / channel), never anyone else's content.
        "slack_react",
        "slack_send_artifact",
        "slack_send_message",
        "slack_set_channel_proactivity",
        "task_cancel",
        "task_continue",
        "task_get",
        "task_search",
        "task_wait",
        "file_diff",
        "workspace_publish",
        "list_files",
        "edit_file",
        "move_file",
        "delete_file",
        "read_file",
        "search_files",
        "write_file",
    }
)


def is_trusted_tool(tool_name: str) -> bool:
    """True when a tool's output originates inside Toda."""
    return tool_name in _TRUSTED_TOOL_NAMES or tool_name.startswith("automation_")


def frame_untrusted_tool_result(tool_name: str, content: str) -> str:
    """Preserve a trust boundary around material Toda did not author.

    This adopts the important Hermes pattern (semantic framing of untrusted tool
    output) without importing the personal-agent prompt wholesale into a
    multi-tenant company product.
    """
    if is_trusted_tool(tool_name):
        return content
    safe = re.sub(r"untrusted_tool_result", "untrusted-tool-result", content, flags=re.I)
    return (
        f'<untrusted_tool_result source="{tool_name}">\n'
        + load("untrusted_tool_result_header")
        + f"\n\n{safe}\n</untrusted_tool_result>"
    )
