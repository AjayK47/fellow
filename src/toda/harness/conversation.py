"""Conversation-first Session Manager execution.

A Session turn is not a Task.  The main model may answer directly, use a
small read-only discovery surface, or explicitly promote the request through
``start_task``.  Promotion hands the same Session and authorization envelope
to the durable Task executor; there is no intent classifier in front of the
model.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from toda.domain.models import new_id
from toda.harness.model_client import instrument_model
from toda.harness.models import (
    AgentOutcome,
    AgentRunState,
    InvocationEnvelope,
    ToolDefinition,
    TriggerKind,
)
from toda.harness.prompting.catalog import load
from toda.harness.prompts import render_voice
from toda.services.readiness import enforce_real_world_readiness

log = logging.getLogger("toda.conversation")


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
        "- ANY work through a connected app (CRM, email, GitHub, calendar). "
        "You can see what is connected with integration_list, but "
        "finding the right action and running it belongs in a Task -- never "
        "search the web to work out what a connected app can do.\n"
        "- A consequential external write, or a durable artifact "
        "(document, chart, report)\n"
        "- Work that would flood this turn with intermediate data\n\n"
        "WHEN NOT TO USE (answer here instead):\n"
        "- A greeting, a thank-you, or a question you can already answer\n"
        "- One lookup that settles it — a single web_search, web_fetch, or "
        "memory_search, then a reply\n"
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

# What a conversation turn may reach for.
#
# The line is NOT read-vs-write -- it is *settles a question* vs *does the
# work*. A conversation turn has no Task behind it: no durability across a
# restart, no delegation, no progress in the channel, no plan. So it gets
# the tools that answer something in a call or two, or that tell it whether
# a Task is needed at all. Everything else is what `start_task` is for.
#
# This was briefly inverted into a denylist of Task-scoped tools, which
# admitted 37 tools instead of 15. The safety argument for that was sound --
# a read cannot damage anything -- but safety was the wrong axis. With
# connector search, document readers and the rest in hand, the model could
# run twelve iterations of genuine work in a lane with none of a Task's
# machinery, and the promotion guard never fired because nothing it wanted
# was outside the set. Observed directly: a request that should have been
# two or three calls became a long inline grind with no Task ever created.
#
# The actual bug that inversion was fixing was one missing tool:
# `integration_list`. Without it the model could not see that
# HubSpot was connected and told a user there was no CRM. That one is back,
# deliberately -- knowing *what exists* is discovery, and it is exactly what
# decides whether to promote. Searching for a specific connector action and
# reading its schema is the preamble to doing the work, so it is not.
_CONVERSATION_READ_TOOLS = {
    # Company memory: usually one lookup settles it.
    "memory_search",
    "memory_read_source",
    "memory_query_facts",
    "case_list",
    "judgment_guidance",
    "owner_resolve",
    "procedure_find",
    "artifact_list",
    "artifact_read",
    # What Toda can do, and what it has already done.
    "skill_list",
    "skill_read",
    "task_search",
    "task_get",
    "session_search",
    # Connector inventory is safe in the conversation lane. Action discovery,
    # schema loading, and execution belong to a durable Task so the lane does
    # not accumulate an integration workflow without a durable owner.
    "integration_list",
    "automation_list",
    # One external lookup that settles a question.
    "web_search",
    "web_fetch",
    # Arithmetic, so a number in a reply is computed rather than guessed.
    "calculate",
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
        # A conversation turn that cannot land in a few calls is a Task
        # wearing the wrong clothes. Keeping this tight is what makes the
        # model reach for start_task instead of grinding inline.
        max_iterations: int = 5,
        max_tokens: int = 8_000,
        readiness_target: Any | None = None,
        persona_name: str = "Toda",
    ) -> None:
        self.store = store
        self.model = model
        self.tools = tools
        self.context_service = context_service
        self.task_agent_factory = task_agent_factory
        self.delivery_enqueue = delivery_enqueue
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens
        self.readiness_target = readiness_target
        self.persona_name = persona_name

    def _ensure_task_start_allowed(self, envelope: InvocationEnvelope, trigger_kind: TriggerKind) -> None:
        if self.readiness_target is not None:
            enforce_real_world_readiness(
                self.readiness_target,
                principal_id=envelope.principal_id,
                trigger_kind=str(trigger_kind),
            )


    def _acknowledge(self, task: dict[str, Any]) -> None:
        """Say we heard them, before the Task's first reasoning turn.

        Hooked here rather than only in SessionManagerScheduler because in
        practice this is where root Tasks are born: the ConversationAgent
        runs first for any session without one, so every Task promoted by
        start_task or by tool selection is created on these paths. Wiring
        the acknowledgement only into the scheduler meant it never fired
        once -- no delivery.acknowledge job was ever enqueued.
        """
        try:
            self.delivery_enqueue(
                organization_id=task["organization_id"],
                kind="delivery.acknowledge",
                payload={"task_id": task["id"]},
                idempotency_key=f"delivery.acknowledge:{task['id']}",
            )
        except Exception:
            log.warning(
                "Could not enqueue acknowledgement for task_id=%s",
                task.get("id"), exc_info=True,
            )

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
            self._ensure_task_start_allowed(envelope, trigger_kind)
            task_agent = self.task_agent_factory(runs)
            root = runs.create_run(
                session_id=session["id"],
                envelope=envelope,
                goal=user_text,
                trigger_kind=trigger_kind,
                trigger_event_id=trigger_event_id,
                max_iterations=task_agent.max_iterations,
                max_tool_calls=task_agent.max_tool_calls,
                max_model_calls=getattr(task_agent, "max_model_calls", 100),
                max_cost_usd=getattr(task_agent, "max_cost_usd", 10.0),
                max_duration_seconds=getattr(task_agent, "max_duration_seconds", 3_600),
                max_total_tokens=getattr(task_agent, "max_total_tokens", 100_000),
                metadata=task_metadata,
            )
            self._acknowledge(root)
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
            "event_correlation_id": envelope.correlation_id,
            "metadata": {**task_metadata, "conversation_turn": True},
        }
        pseudo_run["metadata"]["tool_profile"] = "conversation"
        definitions = [
            definition
            for definition in self.tools.allowed_definitions(pseudo_run)
            if definition.name in _CONVERSATION_READ_TOOLS
            and definition.risk == "read"
            and definition.runtime_ready
        ]
        # The single source of truth for "what can be used without a Task".
        # The promotion guard below reads the same set, so a tool can never
        # be offered here yet treated as Task-only when the model calls it.
        conversation_tools = {item.name for item in definitions}
        schemas = [_START_TASK.openai_schema(), *[item.openai_schema() for item in definitions]]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": render_voice(self.persona_name)},
            {"role": "system", "content": load("core")},
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
            active_model = instrument_model(
                active_model,
                telemetry=self.store.telemetry,
                role="conversation",
                catalog=getattr(active_model, "catalog", None),
            )
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
                    correlation_id=envelope.correlation_id,
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
                    self._ensure_task_start_allowed(envelope, trigger_kind)
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
                        max_model_calls=getattr(task_agent, "max_model_calls", 100),
                        max_cost_usd=getattr(task_agent, "max_cost_usd", 10.0),
                        max_duration_seconds=getattr(task_agent, "max_duration_seconds", 3_600),
                        max_total_tokens=getattr(task_agent, "max_total_tokens", 100_000),
                        metadata=task_metadata,
                    )
                    self._acknowledge(root)
                    return task_agent.execute_task(root["id"])
                if call.name not in conversation_tools:
                    # The model selected a capability that requires durable
                    # state without explicitly calling start_task first. The
                    # runtime owns this boundary: promote the complete request
                    # and replay the already-selected calls as the Task's first
                    # reasoning response instead of discarding or duplicating
                    # them.
                    self._ensure_task_start_allowed(envelope, trigger_kind)
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
                        max_model_calls=getattr(task_agent, "max_model_calls", 100),
                        max_cost_usd=getattr(task_agent, "max_cost_usd", 10.0),
                        max_duration_seconds=getattr(task_agent, "max_duration_seconds", 3_600),
                        max_total_tokens=getattr(task_agent, "max_total_tokens", 100_000),
                        metadata=metadata,
                    )
                    self._acknowledge(root)
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
        # Out of budget without an answer. That is the definition of work
        # that belongs in a Task, so promote instead of raising.
        #
        # Raising here failed the dispatch job, which retried, which re-ran
        # the whole turn from scratch -- so a request the conversation lane
        # could not finish became a loop rather than a Task. Observed live:
        # the model wanted HubSpot's action list, could not reach
        # integration_search from this lane, improvised with Brave and
        # Firecrawl against Composio's own docs, exhausted its budget, and
        # started over.
        log.info(
            "Conversation turn exhausted its budget; promoting to a Task: session=%s",
            session["id"],
        )
        self._ensure_task_start_allowed(envelope, trigger_kind)
        task_agent = self.task_agent_factory(runs)
        root = runs.create_run(
            session_id=session["id"],
            envelope=envelope,
            goal=user_text,
            trigger_kind=trigger_kind,
            trigger_event_id=trigger_event_id,
            max_iterations=task_agent.max_iterations,
            max_tool_calls=task_agent.max_tool_calls,
            max_model_calls=getattr(task_agent, "max_model_calls", 100),
            max_cost_usd=getattr(task_agent, "max_cost_usd", 10.0),
            max_duration_seconds=getattr(task_agent, "max_duration_seconds", 3_600),
            max_total_tokens=getattr(task_agent, "max_total_tokens", 100_000),
            metadata={**task_metadata, "promoted_reason": "conversation_budget_exhausted"},
        )
        self._acknowledge(root)
        return task_agent.execute_task(root["id"])

    def _organization_memory(self, organization_id: str) -> str:
        organization = self.store.get_organization(organization_id) or {}
        memory = str(organization.get("memory_md") or "").strip()
        if memory:
            return memory[:4_000]
        name = str(organization.get("name") or organization_id)
        about = str(organization.get("about") or "").strip()
        return f"# {name}\n\n{about}"[:4_000]
