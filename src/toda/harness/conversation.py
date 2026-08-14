"""Conversation-first Session Manager execution.

A Session turn is not a Task.  The main model may answer directly, use a
small read-only discovery surface, or explicitly promote the request through
``start_task``.  Promotion hands the same Session and authorization envelope
to the durable Task executor; there is no intent classifier in front of the
model.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from toda.domain.models import new_id
from toda.harness.models import (
    AgentOutcome,
    AgentRunState,
    InvocationEnvelope,
    ToolDefinition,
    TriggerKind,
)
from toda.harness.prompt_library import load


_START_TASK = ToolDefinition(
    name="start_task",
    description=(
        "Promote the user's request to one durable root Task, which you own "
        "end to end: it can delegate to specialist workers, post progress, "
        "survive a restart, and is remembered.\n\n"
        "WHEN TO USE start_task:\n"
        "- Work you expect to take more than about a minute\n"
        "- Research or synthesis: several searches or sources to read, "
        "compare, and write up\n"
        "- Anything needing delegation, approval, waiting, or a timer\n"
        "- A consequential external write, or a durable artifact "
        "(document, chart, report)\n"
        "- Work that would flood this turn with intermediate data\n\n"
        "WHEN NOT TO USE (answer here instead):\n"
        "- A greeting, a thank-you, or a question you can already answer\n"
        "- One lookup that settles it — a single web_search, web_fetch, or "
        "brain_search, then a reply\n"
        "- A clarifying question back to the person\n\n"
        "Prefer a Task when genuinely unsure: a short answer arriving from a "
        "Task costs a few seconds, whereas a long investigation stuck in a "
        "conversation turn cannot report progress, delegate, or resume."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "The complete outcome Toda owns, not a single step.",
            }
        },
        "required": ["goal"],
        "additionalProperties": False,
    },
    risk="read",
    capability_id="task.start",
)

# These tools are safe and useful before a Task exists.  Mechanical writes,
# planning, delegation, approvals and artifact creation require start_task.
_CONVERSATION_READ_TOOLS = {
    "brain_search",
    "brain_read_source",
    "brain_query_facts",
    "brain_list_cases",
    "brain_judgment_guidance",
    "brain_resolve_owner",
    "brain_find_skills",
    "brain_list_artifacts",
    "brain_read_artifact",
    "skills_list",
    "skill_view",
    "web_search",
    "web_fetch",
    "task_search",
    "task_get",
}


class ConversationAgent:
    def __init__(
        self,
        *,
        store: Any,
        model: Any | None,
        tools: Any,
        context_service: Any,
        task_agent_factory: Callable[[Any], Any],
        delivery_enqueue: Callable[..., dict[str, Any]],
        max_iterations: int = 12,
        max_tokens: int = 8_000,
    ) -> None:
        self.store = store
        self.model = model
        self.tools = tools
        self.context_service = context_service
        self.task_agent_factory = task_agent_factory
        self.delivery_enqueue = delivery_enqueue
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens

    def run(
        self,
        *,
        session: dict[str, Any],
        envelope: InvocationEnvelope,
        runs: Any,
        trigger_kind: TriggerKind,
        trigger_event_id: str | None,
        task_metadata: dict[str, Any],
        model: Any | None = None,
    ) -> AgentOutcome:
        turn_id = new_id("turn")
        history = self.store.list_session_messages(session["id"], limit=30)
        latest_user = next(
            (item for item in reversed(history) if item.get("role") in {"user", "event"}),
            None,
        )
        user_text = str((latest_user or {}).get("content") or "")
        if task_metadata.get("force_task") or trigger_kind is not TriggerKind.REACTIVE:
            task_agent = self.task_agent_factory(runs)
            root = runs.create_run(
                session_id=session["id"],
                envelope=envelope,
                goal=user_text,
                trigger_kind=trigger_kind,
                trigger_event_id=trigger_event_id,
                max_iterations=task_agent.max_iterations,
                max_tool_calls=task_agent.max_tool_calls,
                metadata=task_metadata,
            )
            return task_agent.execute_task(root["id"])
        context = self.context_service.prepare_context(
            organization_id=session["organization_id"],
            task=user_text,
            acting_principal_id=envelope.principal_id,
            destination=envelope.destination.model_dump(mode="json"),
            purpose=envelope.purpose,
            token_budget=4_000,
            agent_id="conversation_manager",
            conversation={"session_id": session["id"]},
        ).model_dump(mode="json")
        memory_md = self._organization_memory(session["organization_id"])
        pseudo_run = {
            "id": turn_id,
            "organization_id": session["organization_id"],
            "session_id": session["id"],
            "parent_task_id": None,
            "root_task_id": turn_id,
            "state": "running",
            "worker_type": "conversation",
            "trigger_kind": trigger_kind.value,
            "goal": user_text,
            "envelope": envelope.model_dump(mode="json"),
            "trace_id": envelope.trace_id,
            "metadata": {**task_metadata, "conversation_turn": True},
        }
        definitions = [
            definition
            for definition in self.tools.allowed_definitions(pseudo_run)
            if definition.name in _CONVERSATION_READ_TOOLS
            and definition.risk == "read"
            and definition.runtime_ready
        ]
        schemas = [_START_TASK.openai_schema(), *[item.openai_schema() for item in definitions]]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": load("stable_system")},
            {"role": "system", "content": load("conversation_manager")},
            {
                "role": "system",
                "content": "<organization_memory>\n" + memory_md + "\n</organization_memory>",
            },
            {
                "role": "system",
                "content": "<company_context>\n"
                + json.dumps(context, sort_keys=True)
                + "\n</company_context>",
            },
        ]
        for item in history:
            if item.get("role") in {"user", "assistant"}:
                messages.append(
                    {"role": item["role"], "content": str(item.get("content") or "")}
                )

        for _ in range(self.max_iterations):
            active_model = model or self.model
            if active_model is None:
                raise RuntimeError("conversation model is not configured")
            response = active_model.generate(
                messages=messages,
                tools=schemas,
                temperature=0.1,
                max_tokens=self.max_tokens,
            )
            if not response.tool_calls:
                answer = str(response.content or "").strip()
                message = runs.add_message(
                    organization_id=session["organization_id"],
                    session_id=session["id"],
                    task_id=None,
                    role="assistant",
                    content=answer,
                    metadata={"conversation_turn_id": turn_id},
                )
                self.delivery_enqueue(
                    organization_id=session["organization_id"],
                    kind="delivery.conversation",
                    payload={"session_id": session["id"], "message_id": message["id"]},
                    idempotency_key=f"delivery.conversation:{message['id']}",
                )
                return AgentOutcome(
                    task_id=None,
                    session_id=session["id"],
                    kind="conversation",
                    state=AgentRunState.COMPLETED,
                    answer=answer,
                    trace_id=envelope.trace_id,
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments),
                            },
                        }
                        for call in response.tool_calls
                    ],
                }
            )
            for call in response.tool_calls:
                if call.name == "start_task":
                    goal = str(call.arguments.get("goal") or user_text).strip()
                    task_agent = self.task_agent_factory(runs)
                    root = runs.create_run(
                        session_id=session["id"],
                        envelope=envelope,
                        goal=goal,
                        trigger_kind=trigger_kind,
                        trigger_event_id=trigger_event_id,
                        max_iterations=task_agent.max_iterations,
                        max_tool_calls=task_agent.max_tool_calls,
                        metadata=task_metadata,
                    )
                    return task_agent.execute_task(root["id"])
                if call.name not in _CONVERSATION_READ_TOOLS:
                    # The model selected a capability that requires durable
                    # state without explicitly calling start_task first. The
                    # runtime owns this boundary: promote the complete request
                    # and replay the already-selected calls as the Task's first
                    # reasoning response instead of discarding or duplicating
                    # them.
                    task_agent = self.task_agent_factory(runs)
                    metadata = dict(task_metadata)
                    metadata["promoted_model_response"] = response.model_dump(mode="json")
                    root = runs.create_run(
                        session_id=session["id"],
                        envelope=envelope,
                        goal=user_text,
                        trigger_kind=trigger_kind,
                        trigger_event_id=trigger_event_id,
                        max_iterations=task_agent.max_iterations,
                        max_tool_calls=task_agent.max_tool_calls,
                        metadata=metadata,
                    )
                    return task_agent.execute_task(root["id"])
                else:
                    result = self.tools.execute(run=pseudo_run, call=call)
                    output = result.output if result.status == "completed" else {
                        "error": result.error or result.status
                    }
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": json.dumps(output, sort_keys=True),
                    }
                )
        raise RuntimeError("conversation_iteration_limit")

    def _organization_memory(self, organization_id: str) -> str:
        organization = self.store.get_organization(organization_id) or {}
        memory = str(organization.get("memory_md") or "").strip()
        if memory:
            return memory[:4_000]
        name = str(organization.get("name") or organization_id)
        about = str(organization.get("about") or "").strip()
        return f"# {name}\n\n{about}"[:4_000]
