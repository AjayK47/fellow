"""Typed contracts shared by gateway, router, runtime, tools, and workers."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from toda.domain.models import new_id, utcnow


class AgentRunState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TriggerKind(str, Enum):
    REACTIVE = "reactive"
    PROACTIVE = "proactive"
    APPROVAL = "approval"
    TOOL_CONTINUATION = "tool_continuation"
    HUMAN_CONTINUATION = "human_continuation"
    AUTOMATION = "automation"
    MEMORY_CAPTURE = "memory_capture"
    CONSOLIDATION = "consolidation"


class AttentionDecision(str, Enum):
    IGNORE = "IGNORE"
    OBSERVE = "OBSERVE"
    UPDATE_WORKING_STATE = "UPDATE_WORKING_STATE"
    ATTACH_TO_EXISTING_TASK = "ATTACH_TO_EXISTING_TASK"
    ADD_TO_DIGEST = "ADD_TO_DIGEST"
    WAKE_MAIN_AGENT = "WAKE_MAIN_AGENT"
    DM_OWNER = "DM_OWNER"
    ESCALATE_EXISTING_TASK = "ESCALATE_EXISTING_TASK"


class DestinationEnvelope(BaseModel):
    type: str
    id: str
    is_private: bool = False
    audience_principal_ids: list[str] = Field(default_factory=list)


class InvocationEnvelope(BaseModel):
    organization_id: str
    principal_id: str
    application_id: str = "toda"
    conversation_id: str
    destination: DestinationEnvelope
    purpose: str
    permission_decision_id: str
    allowed_scope_ids: list[str] = Field(default_factory=list)
    delegated_by: str | None = None
    trace_id: str = Field(default_factory=lambda: new_id("trace"))


class EventActor(BaseModel):
    type: str = "person"
    id: str
    source_id: str | None = None


class EventScope(BaseModel):
    workspace_id: str | None = None
    channel_id: str | None = None
    thread_id: str | None = None
    visibility: str = "organization_public"
    scope_id: str = "scope_org_public"


class NormalizedEvent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("evt"))
    organization_id: str
    source: str
    type: str
    occurred_at: datetime = Field(default_factory=utcnow)
    actor: EventActor
    scope: EventScope
    payload: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str
    is_direct_agent_request: bool = False
    belongs_to_waiting_run: bool = False
    is_approval_action: bool = False
    bot_mentioned: bool = False
    raw_event: dict[str, Any] = Field(default_factory=dict)

    # Explicit event-processing policy (memory ingestion/retrieval plan §4).
    # None means "let resolve_event_policy() apply the connector default" —
    # a caller that already knows better (e.g. a connector-specific adapter)
    # can set these directly and they will be respected as-is.
    wake_policy: Literal["always", "attention", "never"] | None = None
    memory_policy: Literal["immediate", "episode", "evidence_only"] | None = None
    group_key: str | None = None
    group_kind: (
        Literal["slack_thread", "github_issue", "github_pr", "linear_issue", "account", "custom"] | None
    ) = None
    urgency: Literal["normal", "urgent"] = "normal"


class ModelToolCall(BaseModel):
    id: str = Field(default_factory=lambda: new_id("call"))
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    content: str = ""
    tool_calls: list[ModelToolCall] = Field(default_factory=list)
    finish_reason: str = "stop"
    model: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)
    # A reasoning model's deliberation, kept separate from `content` because
    # it is thinking, not an answer: nothing may surface it to a human or
    # fold it into assistant content. It is carried so the transport can
    # replay a thinking-only turn back to the model (see
    # OpenAICompatibleModelClient.generate) and so a failure can say how much
    # thinking came back instead of just "empty".
    reasoning: str = ""
    # OpenRouter's structured, provider-signed form of the same thing. This
    # is the field to replay when present: Anthropic signs its thinking
    # blocks, so the plain text alone is not a valid continuation item.
    reasoning_details: list[dict[str, Any]] = Field(default_factory=list)


class ToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    risk: Literal["read", "write", "destructive"] = "read"
    approval_required: bool = False
    toolkits: list[str] = Field(default_factory=list)
    capability_id: str | None = None
    approval_mode: Literal["never", "on_write", "always", "conditional"] = "on_write"
    allowed_principals: list[str] = Field(default_factory=list)
    allowed_roles: list[str] = Field(default_factory=list)
    allowed_destination_types: list[str] = Field(default_factory=list)
    allowed_trigger_types: list[str] = Field(default_factory=list)
    allowed_organization_ids: list[str] = Field(default_factory=list)
    main_agent_visible: bool = True
    child_worker_only: bool = False
    connector_readiness: dict[str, Any] = Field(default_factory=dict)
    runtime_ready: bool = True
    setup_needed: list[str] = Field(default_factory=list)
    idempotency_required: bool = False
    max_result_chars: int = 20_000
    timeout_seconds: float = 60.0
    retry_policy: dict[str, Any] = Field(default_factory=dict)
    audit_redaction: list[str] = Field(default_factory=list)

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class ToolResult(BaseModel):
    call_id: str
    tool_name: str
    status: Literal[
        "completed",
        "failed",
        "waiting_for_approval",
        "waiting_for_child_approval",
        "waiting_for_children",
        "denied",
        "disabled",
    ]
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    approval_id: str | None = None
    evidence_source_ids: list[str] = Field(default_factory=list)


class AttentionEvaluation(BaseModel):
    signal_type: str = "none"
    novelty: str = "unknown"
    urgency: str = "low"
    decision: AttentionDecision = AttentionDecision.OBSERVE
    confidence: float = 0.0
    reason: str
    suggested_goal: str | None = None
    related_entity_ids: list[str] = Field(default_factory=list)
    existing_task_id: str | None = None
    matched_role_id: str | None = None


class AgentOutcome(BaseModel):
    task_id: str | None = None
    session_id: str | None = None
    kind: Literal["conversation", "task"] = "task"
    state: AgentRunState
    answer: str | None = None
    approval_id: str | None = None
    error: str | None = None
    trace_id: str
