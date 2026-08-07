"""Cache-friendly prompt construction with explicit trust boundaries.

Prompt *text* lives in prompts.md (see prompt_library); this module owns only
the assembly -- message order, trust-boundary framing, and which optional
blocks apply to a given run.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from docket.harness.prompt_library import load, render


STABLE_SYSTEM_PROMPT = load("stable_system")
STEER_GUIDANCE = load("steer_guidance")
AUTOMATION_TRIGGER_GUIDANCE = load("automation_trigger_guidance")


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
        "budgets": {
            "max_iterations": run["max_iterations"],
            "max_tool_calls": run["max_tool_calls"],
            "used_iterations": run.get("iteration_count", 0),
            "used_tool_calls": run.get("tool_call_count", 0),
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
    invocation["current_time"] = {
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
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": STABLE_SYSTEM_PROMPT},
        {
            "role": "system",
            "content": "<trusted_invocation>\n"
            + json.dumps(invocation, sort_keys=True)
            + "\n</trusted_invocation>",
        },
    ]
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
    if conversation_summary:
        from docket.harness.history_compaction import SUMMARY_PREFIX

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


def frame_untrusted_tool_result(tool_name: str, content: str) -> str:
    """Preserve a trust boundary around attacker-controlled web material.

    This adopts the important Hermes pattern (semantic framing of untrusted tool
    output) without importing the personal-agent prompt wholesale into a
    multi-tenant company product.
    """
    if tool_name not in {"web_search", "web_fetch"}:
        return content
    safe = re.sub(r"untrusted_tool_result", "untrusted-tool-result", content, flags=re.I)
    return (
        f'<untrusted_tool_result source="{tool_name}">\n'
        + load("untrusted_tool_result_header")
        + f"\n\n{safe}\n</untrusted_tool_result>"
    )
