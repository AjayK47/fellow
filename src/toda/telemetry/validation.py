"""Deterministic validation of logical-run span and lifecycle coverage."""

from __future__ import annotations

from typing import Any


_TERMINAL_TOOL_STATES = {
    "completed", "failed", "denied", "disabled",
    "waiting_for_approval", "waiting_for_children",
}
_TERMINAL_QUEUE_STATES = {"completed", "failed", "dead_letter", "cancelled"}
_FORBIDDEN_KEYS = {"prompt", "messages", "content", "arguments", "body", "response"}


def _has_forbidden_key(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in _FORBIDDEN_KEYS:
                return str(key)
            found = _has_forbidden_key(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _has_forbidden_key(child)
            if found:
                return found
    return None


def validate_trace(
    spans: list[dict[str, Any]],
    *,
    delivery_required: bool = False,
    task: dict[str, Any] | None = None,
    tool_records: list[dict[str, Any]] | None = None,
    queue_jobs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate a run without inspecting business payloads.

    ``pending`` is distinct from ``incomplete``: an approval or required
    delivery can leave a healthy run awaiting its next lifecycle segment.
    """
    names = [str(item.get("name") or "") for item in spans]
    missing: list[str] = []
    if "agent.invoke" not in names:
        missing.append("agent.invoke")
    if not any(name == "model.generate" for name in names):
        missing.append("model.generate")
    delivery_missing = delivery_required and "delivery.send" not in names
    if delivery_missing:
        missing.append("delivery.send")

    # Extraction/validation calls are part of the trace, but they are not
    # application model-call budget units.  Keep this definition aligned
    # with RunInspectionService and the runtime usage counter.
    model_spans = [
        item
        for item in spans
        if item.get("name") == "model.generate"
        and str((item.get("attributes") or {}).get("toda.model.role") or "main")
        in {"main", "conversation"}
    ]
    agent_spans = [item for item in spans if item.get("name") == "agent.invoke"]
    tool_spans = [item for item in spans if item.get("name") == "tool.execute"]
    for span in model_spans:
        attrs = span.get("attributes") or {}
        if not attrs.get("toda.provider.id") or not attrs.get("toda.provider_model.id"):
            missing.append(f"model.identity:{span.get('span_id')}")
        if attrs.get("toda.cost.state") not in {"actual", "estimated", "unavailable"}:
            missing.append(f"model.cost_state:{span.get('span_id')}")
    for span in agent_spans:
        attrs = span.get("attributes") or {}
        for key in (
            "toda.prompt.hash", "toda.toolset.hash", "toda.skillset.hash",
            "toda.policy.hash", "toda.catalog.version",
        ):
            if not attrs.get(key):
                missing.append(f"configuration:{key}")

    if task is not None:
        expected_calls = (task.get("usage") or {}).get("model_calls")
        if isinstance(expected_calls, (int, float)) and int(expected_calls) != len(model_spans):
            promoted = int((task.get("metadata") or {}).get("promoted_model_calls") or 0)
            if int(expected_calls) != len(model_spans) + promoted:
                missing.append("model_call_count")

    records = tool_records or []
    if records:
        indexed_call_ids = {
            str((span.get("attributes") or {}).get("toda.tool.call_id"))
            for span in tool_spans
            if (span.get("attributes") or {}).get("toda.tool.call_id")
        }
        span_tool_names = [
            str((span.get("attributes") or {}).get("toda.tool.name") or "")
            for span in tool_spans
        ]
        for record in records:
            state = str(record.get("status") or "")
            if state not in _TERMINAL_TOOL_STATES:
                missing.append(f"tool_not_terminal:{record.get('id')}")
                continue
            call_id = str(record.get("call_id") or record.get("id") or "")
            tool_name = str(record.get("tool_name") or "")
            if call_id not in indexed_call_ids and tool_name not in span_tool_names:
                missing.append(f"tool_span:{call_id or tool_name}")

    for job in queue_jobs or []:
        state = str(job.get("status") or "")
        if state not in _TERMINAL_QUEUE_STATES:
            missing.append(f"queue_not_terminal:{job.get('id')}")

    for span in spans:
        if span.get("end_time_ns") is None:
            missing.append(f"open_span:{span.get('span_id')}")
        for container_name in ("attributes", "events", "links"):
            forbidden = _has_forbidden_key(span.get(container_name) or {})
            if forbidden:
                missing.append(f"forbidden_payload:{forbidden}")

    unique_missing = sorted(set(missing))
    provisional_only_delivery = delivery_missing and len(unique_missing) == 1
    waiting_state = str((task or {}).get("state") or "") == "waiting"
    if provisional_only_delivery or (waiting_state and not unique_missing):
        status = "pending"
    elif unique_missing:
        status = "incomplete"
    else:
        status = "complete"
    return {
        "status": status,
        "complete": status == "complete",
        "span_count": len(spans),
        "required": ["agent.invoke", "model.generate"]
        + (["delivery.send"] if delivery_required else []),
        "missing": unique_missing,
        "validator_version": "2",
    }
