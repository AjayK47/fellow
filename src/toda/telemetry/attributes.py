"""Centralized Toda telemetry names.

OpenTelemetry GenAI conventions are still evolving. Application code should
use these constants and helpers instead of scattering experimental attribute
strings throughout the harness.
"""

from __future__ import annotations

from typing import Any


SPAN_CHANNEL_RECEIVE = "channel.receive"
SPAN_QUEUE_PUBLISH = "queue.publish"
SPAN_QUEUE_PROCESS = "queue.process"
SPAN_AGENT_INVOKE = "agent.invoke"
SPAN_ATTENTION_CLASSIFY = "attention.classify"
SPAN_CONTEXT_BUILD = "context.build"
SPAN_RETRIEVAL_SEARCH = "retrieval.search"
SPAN_MODEL_GENERATE = "model.generate"
SPAN_MODEL_EMBED = "model.embed"
SPAN_TOOL_EXECUTE = "tool.execute"
SPAN_CONNECTOR_REQUEST = "connector.request"
SPAN_APPROVAL_REQUEST = "approval.request"
SPAN_APPROVAL_RESOLVE = "approval.resolve"
SPAN_CHILD_TASK_CREATE = "child_task.create"
SPAN_CHILD_TASK_EXECUTE = "child_task.execute"
SPAN_MEMORY_CAPTURE = "memory.capture"
SPAN_DELIVERY_SEND = "delivery.send"
SPAN_MEDIA_GENERATE = "media.generate"
SPAN_RUN_RESUME = "run.resume"

TODA_ORGANIZATION_ID = "toda.organization.id"
TODA_SESSION_ID = "toda.session.id"
TODA_TASK_ID = "toda.task.id"
TODA_ROOT_TASK_ID = "toda.root_task.id"
TODA_JOB_ID = "toda.job.id"
TODA_JOB_KIND = "toda.job.kind"
TODA_AGENT_TYPE = "toda.agent.type"
TODA_AGENT_ROLE = "toda.agent.role"
TODA_AGENT_ITERATION = "toda.agent.iteration"
TODA_WORKER_ID = "toda.worker.id"
TODA_APPROVAL_ID = "toda.approval.id"
TODA_APPROVAL_STATE = "toda.approval.state"
TODA_OPERATION_OUTCOME = "toda.operation.outcome"
TODA_OPERATION_RISK = "toda.operation.risk"
TODA_PROMPT_VERSION = "toda.prompt.version"
TODA_PROMPT_HASH = "toda.prompt.hash"
TODA_TOOLSET_HASH = "toda.toolset.hash"
TODA_SKILLSET_HASH = "toda.skillset.hash"
TODA_POLICY_HASH = "toda.policy.hash"
TODA_CATALOG_VERSION = "toda.catalog.version"
TODA_CATALOG_FETCHED_AT = "toda.catalog.fetched_at"
TODA_COST_AMOUNT = "toda.cost.amount"
TODA_COST_CURRENCY = "toda.cost.currency"
TODA_COST_SOURCE = "toda.cost.source"
TODA_RUN_OUTCOME = "toda.run.outcome"
TODA_QUEUE_DELAY_MS = "toda.queue.delay_ms"
TODA_ATTEMPT = "toda.attempt"
TODA_PAYLOAD_CAPTURED = "toda.debug.payload_captured"
TODA_PAYLOAD_HASH = "toda.debug.payload_hash"
TODA_INPUT_CHARS = "toda.input.chars"
TODA_OUTPUT_CHARS = "toda.output.chars"
TODA_MODEL_ROLE = "gen_ai.operation.role"
TODA_SYSTEM = "gen_ai.system"
TODA_REQUEST_MODEL = "gen_ai.request.model"
TODA_RESPONSE_MODEL = "gen_ai.response.model"
TODA_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
TODA_REQUEST_TEMPERATURE = "gen_ai.request.temperature"
TODA_USAGE_INPUT = "gen_ai.usage.input_tokens"
TODA_USAGE_OUTPUT = "gen_ai.usage.output_tokens"
TODA_USAGE_TOTAL = "gen_ai.usage.total_tokens"
TODA_USAGE_CACHED = "gen_ai.usage.cached_input_tokens"
TODA_USAGE_REASONING = "gen_ai.usage.reasoning_tokens"


_CORRELATION_KEYS = {
    "organization_id": TODA_ORGANIZATION_ID,
    "session_id": TODA_SESSION_ID,
    "task_id": TODA_TASK_ID,
    "root_task_id": TODA_ROOT_TASK_ID,
    "job_id": TODA_JOB_ID,
    "job_kind": TODA_JOB_KIND,
    "agent_type": TODA_AGENT_TYPE,
    "agent_role": TODA_AGENT_ROLE,
    "iteration": TODA_AGENT_ITERATION,
    "worker_id": TODA_WORKER_ID,
    "approval_id": TODA_APPROVAL_ID,
    "approval_state": TODA_APPROVAL_STATE,
    "risk": TODA_OPERATION_RISK,
    "prompt_version": TODA_PROMPT_VERSION,
    "prompt_hash": TODA_PROMPT_HASH,
    "toolset_hash": TODA_TOOLSET_HASH,
    "skillset_hash": TODA_SKILLSET_HASH,
    "policy_hash": TODA_POLICY_HASH,
    "catalog_version": TODA_CATALOG_VERSION,
    "catalog_fetched_at": TODA_CATALOG_FETCHED_AT,
    "cost_amount": TODA_COST_AMOUNT,
    "cost_currency": TODA_COST_CURRENCY,
    "cost_source": TODA_COST_SOURCE,
    "run_outcome": TODA_RUN_OUTCOME,
}


def correlation_attributes(values: dict[str, Any]) -> dict[str, Any]:
    """Translate safe Toda context keys to indexed telemetry attributes."""

    attrs: dict[str, Any] = {}
    for key, value in values.items():
        if value in (None, ""):
            continue
        target = _CORRELATION_KEYS.get(key)
        if target is not None and isinstance(value, (str, int, float, bool)):
            attrs[target] = value
        elif key.startswith("toda.") and isinstance(value, (str, int, float, bool)):
            attrs[key] = value
    return attrs
