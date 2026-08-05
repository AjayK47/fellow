"""Durable Hermes-style CompanyAgent model/tool loop."""

from __future__ import annotations

import json
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from docket.domain.models import ContextPackage, new_id, utcnow
from docket.harness.model_client import _DEFAULT_CONTEXT_LENGTH, ModelClient
from docket.harness.context_budget import request_budget
from docket.harness.models import (
    AgentOutcome,
    AgentRunState,
    InvocationEnvelope,
    ModelToolCall,
    ToolResult,
    TriggerKind,
)
from docket.harness.prompts import build_agent_messages, frame_untrusted_tool_result
from docket.harness.task_run_store import TaskRunStore
from docket.harness.tools import ToolExecutor
from docket.services.scope_resolution import resolve_scope

log = logging.getLogger("docket.runtime")

# H-05 follow-up: compaction trigger ported from Hermes's context_compressor —
# a threshold relative to the configured model's actual context window,
# not a number picked independent of which model is running. Hermes's
# default is 50% of context, raised to a 75% floor for models under this
# window size (most real models today are under it, so 75% is the
# practical default for almost everyone; only very-large-context models
# get the more conservative 50%).
_SMALL_CTX_WINDOW_LIMIT = 512_000
_SMALL_CTX_THRESHOLD_PERCENT = 0.75
_DEFAULT_THRESHOLD_PERCENT = 0.5
_CHARS_PER_TOKEN_ESTIMATE = 4


def _model_context_length(model: Any) -> int:
    """context_length() is part of the ModelClient protocol, but ad-hoc
    test doubles and other lightweight callers may not implement it —
    compaction timing degrading to a fixed default is a far better failure
    mode than the whole run loop breaking on an AttributeError."""
    getter = getattr(model, "context_length", None)
    if not callable(getter):
        return _DEFAULT_CONTEXT_LENGTH
    try:
        return int(getter())
    except Exception:
        return _DEFAULT_CONTEXT_LENGTH


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Compatibility wrapper for callers that only need history estimates."""
    from docket.harness.context_budget import estimate_tokens

    return estimate_tokens(messages)


class CompanyAgent:
    def __init__(
        self,
        *,
        runs: TaskRunStore,
        context_service: Any,
        model: ModelClient,
        tools: ToolExecutor,
        skills: Any,
        skill_validator: Any,
        audit: Any,
        enqueue_job: Callable[..., dict[str, Any]],
        graph: Any | None = None,
        history_compactor: Any | None = None,
        workspaces: Any | None = None,
        max_iterations: int = 12,
        max_tool_calls: int = 24,
        context_token_budget: int = 12_000,
        max_total_tokens: int = 100_000,
        completion_max_tokens: int = 40_000,
    ) -> None:
        self.runs = runs
        self.context_service = context_service
        self.model = model
        self.tools = tools
        self.skills = skills
        self.skill_validator = skill_validator
        self.audit = audit
        self.enqueue_job = enqueue_job
        self.graph = graph
        self.history_compactor = history_compactor
        self.workspaces = workspaces
        self.max_iterations = max_iterations
        self.max_tool_calls = max_tool_calls
        self.context_token_budget = context_token_budget
        self.max_total_tokens = max_total_tokens
        self.completion_max_tokens = completion_max_tokens

    def create_invocation(
        self,
        *,
        envelope: InvocationEnvelope,
        goal: str,
        trigger_kind: TriggerKind | str,
        trigger_event_id: str | None = None,
        initial_message: str | None = None,
        parent_run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self.runs.get_or_create_session(
            organization_id=envelope.organization_id,
            conversation_key=envelope.conversation_id,
            destination=envelope.destination.model_dump(mode="json"),
            participants=[envelope.principal_id],
            metadata=metadata,
        )
        active = self.runs.store.find_root_task_for_session(session["id"])
        if active and active["state"] in {
            AgentRunState.WAITING_FOR_HUMAN.value,
            AgentRunState.WAITING_FOR_APPROVAL.value,
            AgentRunState.PAUSED.value,
        }:
            run = active
            if initial_message:
                self.runs.add_message(
                    organization_id=envelope.organization_id,
                    session_id=session["id"],
                task_id=run["id"],
                    role="user",
                    content=initial_message,
                    metadata={"trigger_event_id": trigger_event_id},
                )
            return run
        run = self.runs.create_run(
            session_id=session["id"],
            envelope=envelope,
            goal=goal,
            trigger_kind=trigger_kind,
            trigger_event_id=trigger_event_id,
            parent_run_id=parent_run_id,
            max_iterations=self.max_iterations,
            max_tool_calls=self.max_tool_calls,
            metadata=metadata,
        )
        if self.workspaces:
            workspace = self.workspaces.get_or_create(
                organization_id=envelope.organization_id,
                owner_id=envelope.principal_id,
                scope_id=resolve_scope(
                    store=self.runs.store,
                    organization_id=envelope.organization_id,
                    principal_id=envelope.principal_id,
                    destination=envelope.destination.model_dump(mode="json"),
                ).write_scope_id,
                slack_workspace_id=((run.get("metadata") or {}).get("slack") or {}).get(
                    "workspace_id"
                ),
            )
            run_metadata = dict(run.get("metadata") or {})
            run_metadata["workspace_id"] = workspace["id"]
            run["metadata"] = run_metadata
            self.runs.update(run)
        if initial_message or goal:
            self.runs.add_message(
                organization_id=envelope.organization_id,
                session_id=session["id"],
                    task_id=run["id"],
                role="user",
                content=initial_message or goal,
                metadata={"trigger_event_id": trigger_event_id},
            )
        if self.graph:
            self.graph.upsert_entity(
                {
                    "id": run["id"],
                    "organization_id": envelope.organization_id,
                    "entity_type": "task",
                    "name": f"Task: {goal[:80]}",
                    "aliases": [],
                    "attributes": {
                        "state": run["state"],
                        "trigger_kind": run["trigger_kind"],
                        "trace_id": run["trace_id"],
                    },
                }
            )
            self.graph.add_edge(
                envelope.organization_id,
                "EXECUTED_BY",
                run["id"],
                envelope.principal_id,
            )
            if parent_run_id:
                self.graph.add_edge(
                    envelope.organization_id,
                    "DELEGATED_TO",
                    parent_run_id,
                    run["id"],
                )
        return self.runs.transition(run["id"], AgentRunState.QUEUED)

    def execute_task(self, run_id: str) -> AgentOutcome:
        run = self.runs.get(run_id)
        if not run:
            raise KeyError(f"run not found: {run_id}")
        if run["state"] == AgentRunState.COMPLETED.value:
            return AgentOutcome(
                task_id=run_id,
                state=AgentRunState.COMPLETED,
                answer=run.get("final_answer"),
                trace_id=run["trace_id"],
            )
        if run["state"] in {
            AgentRunState.WAITING_FOR_APPROVAL.value,
            AgentRunState.WAITING_FOR_HUMAN.value,
            AgentRunState.WAITING_FOR_CHILDREN.value,
        }:
            return AgentOutcome(
                task_id=run_id,
                state=AgentRunState(run["state"]),
                approval_id=run.get("pending_approval_id"),
                trace_id=run["trace_id"],
            )
        if run["state"] == AgentRunState.FAILED.value:
            run = self.runs.transition(run_id, AgentRunState.QUEUED)
        log.info(
            "Task starting: task_id=%s goal=%r trigger=%s principal=%s",
            run_id,
            (run.get("goal") or "")[:120],
            run.get("trigger_kind"),
            run["envelope"]["principal_id"],
        )
        run = self.runs.transition(
            run_id, AgentRunState.RUNNING, force=run["state"] == "running"
        )
        return self._run_loop_protected(run)

    def _run_loop_protected(self, run: dict[str, Any]) -> AgentOutcome:
        """Every entrypoint that re-enters _loop() (initial run, resume
        after approval, resume with a human reply) must go through this,
        not call _loop() directly. Without it, any exception mid-loop
        (a model API error, a network failure, anything) leaves the run
        permanently stuck at RUNNING with no failure ever recorded — a
        zombie that nothing will retry and no human will get an answer or
        error from. run() always had this protection; resume_after_approval
        and resume_with_human_message did not, which is exactly how a run
        could hang forever right after a human approved its next step.
        """
        run_id = run["id"]
        try:
            return self._loop(run)
        except Exception as exc:
            failed = self.runs.fail(run_id, str(exc))
            self.audit.record(
                failed["organization_id"],
                "task.failed",
                {"task_id": run_id, "error": str(exc)},
                principal_id=failed["envelope"]["principal_id"],
                trace_id=failed["trace_id"],
            )
            # Whether this failure is worth telling a human about depends on
            # whether the queue is about to retry it (transient errors like
            # a rate limit or timeout resolve themselves silently and
            # shouldn't produce a false alarm) — that policy lives with the
            # job handlers in worker.py, not here. See _run_agent,
            # _human_resume, and _resolve_approval for where the
            # delivery.failure job actually gets enqueued.
            return AgentOutcome(
                task_id=run_id,
                state=AgentRunState.FAILED,
                error=str(exc),
                trace_id=failed["trace_id"],
            )

    def _loop(self, run: dict[str, Any]) -> AgentOutcome:
        # prepare_context() (embeddings included) used to run unconditionally
        # on every iteration -- real cost, and most iterations don't need a
        # fresh fetch. Cache it across iterations, but invalidate whenever
        # the just-completed iteration executed a write-risk tool call, so
        # the model still sees facts/memories it just wrote itself. A fresh
        # `_loop()` call (initial run, or any resume) always starts dirty.
        cached_context: dict[str, Any] | None = None
        context_dirty = True
        while int(run.get("iteration_count", 0)) < int(run["max_iterations"]):
            persisted = self.runs.get(run["id"])
            if persisted and persisted["state"] == AgentRunState.CANCELLED.value:
                return AgentOutcome(
                    task_id=run["id"],
                    state=AgentRunState.CANCELLED,
                    trace_id=run["trace_id"],
                )
            if persisted:
                run = persisted
            # Both the Task executor lease and, for a root Task, the owning
            # Session Manager lease are extended at every durable iteration.
            self.runs.renew(run["id"])
            signal = self.runs.pop_signal(run["id"])
            if signal:
                if signal.get("kind") == "abort":
                    cancelled = self.runs.transition(
                        run["id"],
                        AgentRunState.CANCELLED,
                        event_payload={
                            "reason": "steer_abort",
                            "message": signal.get("message"),
                        },
                    )
                    return AgentOutcome(
                        task_id=run["id"],
                        state=AgentRunState.CANCELLED,
                        trace_id=cancelled["trace_id"],
                    )
                if signal.get("kind") == "reauthorize":
                    paused = self.runs.transition(
                        run["id"],
                        AgentRunState.WAITING_FOR_HUMAN,
                        event_payload={"reason": "current_actor_changed"},
                    )
                    return AgentOutcome(
                        task_id=run["id"],
                        state=AgentRunState.WAITING_FOR_HUMAN,
                        trace_id=paused["trace_id"],
                    )
                # SessionService already persisted ordinary steering in the
                # Session transcript and therefore sends an empty signal.
                # Direct send_signal callers still provide message content,
                # which must become a durable steered turn here.
                if signal.get("kind") == "steer" and signal.get("message"):
                    self.runs.add_message(
                        organization_id=run["organization_id"],
                        session_id=run["session_id"],
                        task_id=run["id"],
                        role="user",
                        content=signal["message"],
                        metadata={"steered": True},
                    )
            if int(run.get("tool_call_count", 0)) >= int(run["max_tool_calls"]):
                raise RuntimeError("tool_call_limit")
            if int((run.get("usage") or {}).get("total_tokens", 0)) >= int(
                run.get("max_total_tokens") or self.max_total_tokens
            ):
                raise RuntimeError("model_token_budget")
            if run.get("deadline_at") and utcnow().isoformat() >= run["deadline_at"]:
                raise RuntimeError("worker_deadline_exceeded")
            active_skills = self._active_skills(run)
            is_child = bool(run.get("parent_task_id"))
            full_history = self.runs.messages(
                run["session_id"],
                limit=200,
                task_id=run["id"] if is_child else None,
            )
            summary = ""
            history = full_history
            context_length = _model_context_length(self.model)
            model_name = getattr(self.model, "model", None)
            prior_budget = (run.get("metadata") or {}).get("request_budget") or {}
            prior_estimate = float(prior_budget.get("estimated_input_tokens") or 0)
            measured_prompt = float(
                (run.get("metadata") or {}).get("last_prompt_tokens") or 0
            )
            # Provider prompt usage includes wrappers that local preflight
            # cannot see. Carry forward only an upward calibration, capped so
            # a single anomalous response cannot starve retrieval forever.
            calibration_factor = 1.0
            if prior_estimate > 0 and measured_prompt > prior_estimate:
                calibration_factor = min(2.0, measured_prompt / prior_estimate)
            allowed = self.tools.allowed_definitions(run)
            schemas = [definition.openai_schema() for definition in allowed]
            # The output cap is a reservation, not an expected duration. It
            # must be subtracted before allocating input/context space.
            provisional_budget = request_budget(
                context_window=context_length,
                output_tokens=self.completion_max_tokens,
                messages=[],
                tools=schemas,
                model=model_name,
                calibration_factor=calibration_factor,
            )
            threshold_percent = (
                _SMALL_CTX_THRESHOLD_PERCENT
                if context_length < _SMALL_CTX_WINDOW_LIMIT
                else _DEFAULT_THRESHOLD_PERCENT
            )
            threshold_tokens = int(
                provisional_budget.effective_input_window * threshold_percent
            )
            if len(full_history) > 30 and _estimate_tokens(full_history) > threshold_tokens:
                session = self.runs.store.get_session(run["session_id"])
                older = full_history[:-30]
                history = full_history[-30:]
                previous_summary = (session or {}).get("summary") or ""
                already_through = (session or {}).get("summary_through_message_id")
                # H-05: only fold in messages that haven't already been
                # summarized — the running summary is merged incrementally,
                # never re-derived from the whole `older` slice each time.
                new_messages = older
                if already_through:
                    for index, message in enumerate(older):
                        if message["id"] == already_through:
                            new_messages = older[index + 1 :]
                            break
                if new_messages:
                    if self.history_compactor is not None:
                        summary = self.history_compactor.compact(
                            previous_summary=previous_summary,
                            new_messages=new_messages,
                        )
                    else:
                        summary = self._compact_history(older)
                else:
                    summary = previous_summary
                if session:
                    session["summary"] = summary
                    session["summary_through_message_id"] = older[-1]["id"]
                    session["updated_at"] = utcnow().isoformat()
                    self.runs.store.update_session(session)
            else:
                session = self.runs.store.get_session(run["session_id"])
                summary = (session or {}).get("summary") or ""

            # Build once without company context to determine how much room is
            # left for permission-filtered retrieval. Tool schemas are part of
            # this estimate, so a large tool registry naturally shrinks the
            # retrieval budget instead of overflowing the provider request.
            preliminary_messages = build_agent_messages(
                run=run,
                context={},
                history=history,
                conversation_summary=summary,
                slack_conversation=self._slack_conversation(run),
                active_skills=active_skills,
                available_skills=(run.get("metadata") or {}).get("skill_index") or [],
            )
            preliminary_budget = request_budget(
                context_window=context_length,
                output_tokens=self.completion_max_tokens,
                messages=preliminary_messages,
                tools=schemas,
                model=model_name,
                calibration_factor=calibration_factor,
            )
            dynamic_context_budget = min(
                self.context_token_budget,
                preliminary_budget.remaining_for_context,
            )

            if run.get("worker_type") in {"general", "pi"} or (run.get("metadata") or {}).get("worker_type") in {"general", "pi"}:
                # Child Tasks already get an explicit prompt from the
                # delegating call and do not inherit broad Session retrieval.
                if cached_context is None:
                    cached_context = ContextPackage(
                        permission_decision_id=run["envelope"]["permission_decision_id"],
                        summary=(
                            "Child worker: context retrieval is skipped by "
                            "design. Use brain_search/brain_query_facts directly "
                            "if you genuinely need to look something up."
                        ),
                    ).model_dump(mode="json")
            elif context_dirty or cached_context is None:
                cached_context = self.context_service.prepare_context(
                    organization_id=run["organization_id"],
                    task=run["goal"],
                    acting_principal_id=run["envelope"]["principal_id"],
                    destination=run["envelope"]["destination"],
                    purpose=run["envelope"]["purpose"],
                    token_budget=dynamic_context_budget,
                    agent_id="company_agent",
                    conversation={"session_id": run["session_id"]},
                ).model_dump(mode="json")
                context_dirty = False
            context = cached_context

            messages = build_agent_messages(
                run=run,
                context=context,
                history=history,
                conversation_summary=summary,
                slack_conversation=self._slack_conversation(run),
                active_skills=active_skills,
                available_skills=(run.get("metadata") or {}).get("skill_index") or [],
            )
            final_budget = request_budget(
                context_window=context_length,
                output_tokens=self.completion_max_tokens,
                messages=messages,
                tools=schemas,
                model=model_name,
                calibration_factor=calibration_factor,
            )
            if not final_budget.fits and not (run.get("metadata") or {}).get(
                "worker_type"
            ):
                # Retrieval is the lowest-priority input. Rebuild it using the
                # exact remaining space before throwing away conversation
                # history, which keeps the active Task and latest user turn
                # intact whenever possible.
                retry_context_budget = max(0, preliminary_budget.remaining_for_context)
                cached_context = self.context_service.prepare_context(
                    organization_id=run["organization_id"],
                    task=run["goal"],
                    acting_principal_id=run["envelope"]["principal_id"],
                    destination=run["envelope"]["destination"],
                    purpose=run["envelope"]["purpose"],
                    token_budget=min(self.context_token_budget, retry_context_budget),
                    agent_id="company_agent",
                    conversation={"session_id": run["session_id"]},
                ).model_dump(mode="json")
                context = cached_context
                messages = build_agent_messages(
                    run=run,
                    context=context,
                    history=history,
                    conversation_summary=summary,
                    slack_conversation=self._slack_conversation(run),
                    active_skills=active_skills,
                    available_skills=(run.get("metadata") or {}).get("skill_index") or [],
                )
                final_budget = request_budget(
                    context_window=context_length,
                    output_tokens=self.completion_max_tokens,
                    messages=messages,
                    tools=schemas,
                    model=model_name,
                    calibration_factor=calibration_factor,
                )
            if not final_budget.fits:
                # Keep the newest conversation evidence and the active prompt;
                # the incremental summary carries the aged-out middle.
                history = history[-8:]
                messages = build_agent_messages(
                    run=run,
                    context=context,
                    history=history,
                    conversation_summary=summary,
                    slack_conversation=self._slack_conversation(run),
                    active_skills=active_skills,
                    available_skills=(run.get("metadata") or {}).get("skill_index") or [],
                )
                final_budget = request_budget(
                    context_window=context_length,
                    output_tokens=self.completion_max_tokens,
                    messages=messages,
                    tools=schemas,
                    model=model_name,
                    calibration_factor=calibration_factor,
                )
            # Tiny synthetic windows used by local compaction tests are not
            # provider-realistic. For real model windows, never send a request
            # that the preflight calculation says cannot fit.
            if not final_budget.fits and context_length >= 4096:
                raise RuntimeError(
                    "context_window_exceeded: "
                    f"estimated={final_budget.estimated_input_tokens} "
                    f"effective={final_budget.effective_input_window}"
                )
            run_metadata = dict(run.get("metadata") or {})
            run_metadata["request_budget"] = {
                "context_window": final_budget.context_window,
                "output_reservation": final_budget.output_reservation,
                "safety_margin": final_budget.safety_margin,
                "effective_input_window": final_budget.effective_input_window,
                "estimated_input_tokens": final_budget.estimated_input_tokens,
                "remaining_for_context": final_budget.remaining_for_context,
                "context_token_budget": dynamic_context_budget,
                "model": model_name,
                "calibration_factor": calibration_factor,
            }
            run_metadata["tool_refs"] = [
                {
                    "name": definition.name,
                    "capability_id": definition.capability_id,
                    "schema_hash": hashlib.sha256(
                        json.dumps(definition.input_schema, sort_keys=True).encode("utf-8")
                    ).hexdigest(),
                    "risk": definition.risk,
                    "approval_mode": definition.approval_mode,
                }
                for definition in allowed
            ]
            for ref in run_metadata["tool_refs"]:
                now = utcnow().isoformat()
                self.runs.store.upsert_harness_projection(
                    "task_capability_refs",
                    {
                        "id": f"{run['id']}:{ref['name']}",
                        "organization_id": run["organization_id"],
                        "task_id": run["id"],
                        "status": "active",
                        **ref,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            run["metadata"] = run_metadata
            run["iteration_count"] = int(run.get("iteration_count", 0)) + 1
            self.runs.update(run, checkpoint_reason="before_model")
            log.info(
                "Agent loop iteration %s/%s: run_id=%s tool_calls=%s/%s",
                run["iteration_count"],
                run["max_iterations"],
                run["id"],
                run.get("tool_call_count", 0),
                run["max_tool_calls"],
            )
            response = self.model.generate(
                messages=messages,
                tools=schemas,
                temperature=0.1,
                max_tokens=self.completion_max_tokens,
            )
            if response.tool_calls:
                response.tool_calls = self._deduplicate_tool_calls(
                    run["id"], response.tool_calls
                )
            usage = dict(run.get("usage") or {})
            response_usage = response.usage or {}
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "cost",
            ):
                try:
                    usage[key] = float(usage.get(key, 0)) + float(
                        response_usage.get(key, 0)
                    )
                except (TypeError, ValueError):
                    continue
            usage["model_calls"] = int(usage.get("model_calls", 0)) + 1
            run["usage"] = usage
            # Preserve the provider's real prompt measurement alongside the
            # cumulative usage. This is the calibration signal for later
            # preflight estimates and remains inspectable in Task detail.
            run_metadata = dict(run.get("metadata") or {})
            run_metadata["last_prompt_tokens"] = response_usage.get("prompt_tokens")
            run_metadata["last_completion_tokens"] = response_usage.get(
                "completion_tokens"
            )
            run_metadata["last_total_tokens"] = response_usage.get("total_tokens")
            run["metadata"] = run_metadata
            self.runs.update(run)
            answer: str | None = None
            skill_validation: dict[str, Any] | None = None
            assistant_content = response.content
            if not response.tool_calls:
                draft = response.content.strip()
                if not draft:
                    raise RuntimeError("model returned neither answer nor tool call")
                skill_validation = self._validate_user_response(
                    run=run,
                    response=draft,
                    active_skills=active_skills,
                )
                answer = str(skill_validation["response"]).strip()
                assistant_content = answer
            self.runs.append_turn(
                run,
                request={
                    "message_count": len(messages),
                    "context_request_id": context["request_id"],
                    "permission_decision_id": context["permission_decision_id"],
                    "tools": [definition.name for definition in allowed],
                    "active_skills": [
                        {
                            "id": skill["id"],
                            "version": skill["version"],
                            "mode": skill["activation_mode"],
                        }
                        for skill in active_skills
                    ],
                    "model": getattr(self.model, "model", ""),
                },
                response={
                    "content": assistant_content,
                    "draft_content": response.content
                    if assistant_content != response.content
                    else None,
                    "skill_validation": {
                        key: value
                        for key, value in (skill_validation or {}).items()
                        if key not in {"response", "usage"}
                    }
                    if skill_validation
                    else None,
                    "tool_calls": [
                        call.model_dump(mode="json") for call in response.tool_calls
                    ],
                    "finish_reason": response.finish_reason,
                    "model": response.model,
                    "usage": response.usage,
                },
            )
            self.runs.add_message(
                organization_id=run["organization_id"],
                session_id=run["session_id"],
                task_id=run["id"],
                role="assistant",
                content=assistant_content,
                tool_calls=[
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
            )
            if not response.tool_calls:
                assert answer is not None
                log.info(
                    "Task completed: task_id=%s iterations=%s tool_calls=%s answer_len=%s",
                    run["id"],
                    run["iteration_count"],
                    run.get("tool_call_count", 0),
                    len(answer),
                )
                completed = self.runs.complete(run["id"], answer)
                if self.graph:
                    self.graph.upsert_entity(
                        {
                            "id": completed["id"],
                            "organization_id": completed["organization_id"],
                            "entity_type": "task",
                            "name": f"Task: {completed['goal'][:80]}",
                            "aliases": [],
                            "attributes": {
                                "state": completed["state"],
                                "trigger_kind": completed["trigger_kind"],
                                "trace_id": completed["trace_id"],
                                "final_answer": answer[:500],
                            },
                        }
                    )
                self._enqueue_post_run_jobs(completed)
                self.audit.record(
                    run["organization_id"],
                    "task.completed",
                    {
                        "task_id": run["id"],
                        "iterations": completed["iteration_count"],
                        "tool_calls": completed["tool_call_count"],
                    },
                    principal_id=run["envelope"]["principal_id"],
                    trace_id=run["trace_id"],
                )
                return AgentOutcome(
                    task_id=run["id"],
                    state=AgentRunState.COMPLETED,
                    answer=answer,
                    trace_id=run["trace_id"],
                )

            # H-08: independent read-only calls run concurrently; anything
            # that can pause the run (writes needing approval, human input)
            # or is otherwise not declared read-safe stays serialized, in
            # its original order, so pause/early-return semantics are never
            # affected by concurrency.
            allowed_by_name = {definition.name: definition for definition in allowed}

            def _is_read_safe(call: ModelToolCall) -> bool:
                # Delegation can pause the Task and therefore stays serialized.
                # risk="read" (a child worker's own write tools still
                # gate normally), but the call itself can come back
                # status="waiting_for_child_approval"/"waiting_for_children"
                # when a child pauses or is dispatched -- that must go
                # through the serialized other_calls path below, not the
                # concurrent read path, which never checks status.
                if call.name in {
                    "request_human_input",
                    "delegate_tasks",
                    "wait_for_children",
                }:
                    return False
                definition = allowed_by_name.get(call.name)
                return bool(definition and definition.risk == "read")

            read_call_ids = {
                call.id for call in response.tool_calls if _is_read_safe(call)
            }
            read_calls = [
                call for call in response.tool_calls if call.id in read_call_ids
            ]
            other_calls = [
                call for call in response.tool_calls if call.id not in read_call_ids
            ]

            if read_calls:
                run["tool_call_count"] = int(run.get("tool_call_count", 0)) + len(
                    read_calls
                )
                self.runs.update(run)
                for call in read_calls:
                    log.info(
                        "Tool call (parallel): run_id=%s tool=%s call_id=%s",
                        run["id"],
                        call.name,
                        call.id,
                    )
                with ThreadPoolExecutor(
                    max_workers=min(len(read_calls), 4),
                    thread_name_prefix="company-agent-tool",
                ) as pool:
                    future_to_call = {
                        pool.submit(self.tools.execute, run=run, call=call): call
                        for call in read_calls
                    }
                    results_by_call_id = {
                        future_to_call[future].id: future.result()
                        for future in as_completed(future_to_call)
                    }
                # Append in the model's original order, not completion order,
                # so the transcript stays deterministic regardless of timing.
                for call in read_calls:
                    self._append_tool_result(run, results_by_call_id[call.id])
                    self._track_checkpoint(run, call, allowed_by_name.get(call.name))

            for call in other_calls:
                run["tool_call_count"] = int(run.get("tool_call_count", 0)) + 1
                self.runs.update(run)
                log.info(
                    "Tool call: run_id=%s tool=%s call_id=%s",
                    run["id"],
                    call.name,
                    call.id,
                )
                definition = allowed_by_name.get(call.name)
                if (
                    call.name != "request_human_input"
                    and definition is not None
                    and definition.risk != "read"
                ):
                    context_dirty = True
                result = self.tools.execute(run=run, call=call)
                if (
                    call.name == "request_human_input"
                    and result.status == "completed"
                ):
                    question = str(result.output.get("question") or "")
                    question_validation = self._validate_user_response(
                        run=run,
                        response=question,
                        active_skills=active_skills,
                    )
                    result.output["question"] = question_validation["response"]
                    run = self.runs.get(run["id"]) or run
                    self._append_tool_result(run, result)
                    run["pending_question"] = result.output.get("question")
                    run["pending_question_choices"] = result.output.get("choices")
                    self.runs.update(run)
                    self.runs.transition(
                        run["id"],
                        AgentRunState.WAITING_FOR_HUMAN,
                        event_payload={
                            "question": result.output.get("question"),
                            "reason": result.output.get("reason"),
                            "choices": result.output.get("choices"),
                        },
                    )
                    if run.get("parent_task_id"):
                        revision = self.runs.store.bump_session_revision(
                            run["session_id"]
                        )
                        self.enqueue_job(
                            organization_id=run["organization_id"],
                            kind="session.dispatch",
                            payload={"session_id": run["session_id"]},
                            idempotency_key=(
                                f"session.dispatch:child_question:{run['id']}:"
                                f"{revision}"
                            ),
                        )
                    else:
                        self.enqueue_job(
                            organization_id=run["organization_id"],
                            kind="delivery.question",
                            payload={
                                "task_id": run["id"],
                                "question": result.output.get("question"),
                                "choices": result.output.get("choices"),
                            },
                            idempotency_key=f"delivery.question:{run['id']}:{call.id}",
                        )
                    return AgentOutcome(
                        task_id=run["id"],
                        state=AgentRunState.WAITING_FOR_HUMAN,
                        answer=result.output.get("question"),
                        trace_id=run["trace_id"],
                    )
                if result.status == "waiting_for_approval":
                    run["pending_approval_id"] = result.approval_id
                    run["pending_tool_call_id"] = call.id
                    run["pending_tool_name"] = call.name
                    self.runs.update(run)
                    self.runs.transition(
                        run["id"],
                        AgentRunState.WAITING_FOR_APPROVAL,
                        event_payload={
                            "approval_id": result.approval_id,
                            "tool_name": call.name,
                        },
                    )
                    return AgentOutcome(
                        task_id=run["id"],
                        state=AgentRunState.WAITING_FOR_APPROVAL,
                        approval_id=result.approval_id,
                        trace_id=run["trace_id"],
                    )
                if result.status == "waiting_for_children":
                    # delegate_tasks already created and enqueued the child
                    # Tasks (child_tasks.py) -- pause here without blocking a
                    # thread on them. resume_for_children() is what wakes
                    # this Task back up, called by the Session Manager
                    # scheduler once a child's terminal transition notifies
                    # the Session.
                    run["pending_child_task_ids"] = result.output.get(
                        "child_task_ids"
                    ) or []
                    run["pending_tool_call_id"] = call.id
                    run["pending_tool_name"] = call.name
                    self.runs.update(run)
                    self.runs.transition(
                        run["id"],
                        AgentRunState.WAITING_FOR_CHILDREN,
                        event_payload={
                            "child_task_ids": run["pending_child_task_ids"],
                            "tool_name": call.name,
                        },
                    )
                    return AgentOutcome(
                        task_id=run["id"],
                        state=AgentRunState.WAITING_FOR_CHILDREN,
                        trace_id=run["trace_id"],
                    )
                self._append_tool_result(run, result)
                self._track_checkpoint(run, call, definition)
            run = self.runs.get(run["id"]) or run
        raise RuntimeError("iteration_limit")

    def resume_after_approval(
        self,
        *,
        approval_id: str,
        approved: bool,
        decided_by: str,
        edited_arguments: dict[str, Any] | None = None,
    ) -> AgentOutcome:
        approval_record = self.runs.store.get_harness_record(
            "approvals", approval_id
        )
        if not approval_record:
            raise KeyError(approval_id)
        run = self.runs.get(approval_record["task_id"])
        if not run:
            raise KeyError(approval_record["task_id"])
        self.runs.transition(run["id"], AgentRunState.RUNNING)
        approval, result = self.tools.resolve_approval(
            approval_id=approval_id,
            approved=approved,
            decided_by=decided_by,
            edited_arguments=edited_arguments,
        )
        run = self.runs.get(approval["task_id"])
        if not run:
            raise KeyError(approval["task_id"])
        run.pop("pending_approval_id", None)
        run.pop("pending_tool_call_id", None)
        run.pop("pending_tool_name", None)
        self.runs.update(run)
        if result:
            self._append_tool_result(run, result)
        outcome = self._run_loop_protected(self.runs.get(run["id"]) or run)
        return outcome

    def resume_with_human_message(
        self,
        run_id: str,
        *,
        message: str,
        principal_id: str,
        persist_message: bool = True,
    ) -> AgentOutcome:
        run = self.runs.get(run_id)
        if not run:
            raise KeyError(run_id)
        if persist_message:
            self.runs.add_message(
                organization_id=run["organization_id"],
                session_id=run["session_id"],
                task_id=run_id,
                role="user",
                content=message,
                metadata={"principal_id": principal_id, "continuation": True},
            )
        self.runs.transition(run_id, AgentRunState.RUNNING)
        return self._run_loop_protected(self.runs.get(run_id) or run)

    def resume_for_children(self, run_id: str) -> AgentOutcome:
        """Wake a delegate_tasks parent paused at WAITING_FOR_CHILDREN.

        Called whenever the Session Manager scheduler sees the root (or any
        ancestor) Task in that state -- typically because a child's terminal
        transition notified the Session, but also safe to call speculatively
        (e.g. a checkpoint tick): each child's CURRENT state/result is
        reported, and _loop() is free to decide "still waiting" is the right
        outcome and pause again on the same or a fresh delegate_tasks call.
        """
        run = self.runs.get(run_id)
        if not run:
            raise KeyError(run_id)
        child_task_ids = run.get("pending_child_task_ids") or []
        children = [
            child
            for child in (self.runs.store.get_task(cid) for cid in child_task_ids)
            if child
        ]
        summaries = [
            {
                "task_id": child["id"],
                "worker_type": child.get("worker_type"),
                "prompt": child.get("prompt") or child.get("goal"),
                "state": child["state"],
                "answer": child.get("final_answer"),
                "error": child.get("error"),
                "wake_revision": child.get("wake_revision", 0),
            }
            for child in children
        ]
        tool_call_id = run.get("pending_tool_call_id")
        tool_name = run.get("pending_tool_name") or "delegate_tasks"
        unfinished = [
            child["id"]
            for child in children
            if child["state"] not in {
                AgentRunState.COMPLETED.value,
                AgentRunState.FAILED.value,
                AgentRunState.CANCELLED.value,
            }
        ]
        self.runs.transition(run["id"], AgentRunState.RUNNING)
        run = self.runs.get(run["id"]) or run
        if unfinished:
            run["pending_child_task_ids"] = child_task_ids
        else:
            run.pop("pending_child_task_ids", None)
        run.pop("pending_tool_call_id", None)
        run.pop("pending_tool_name", None)
        self.runs.update(run)
        if tool_call_id:
            tool_result = ToolResult(
                call_id=tool_call_id,
                tool_name=tool_name,
                status="completed",
                output={
                    "children": summaries,
                    "unfinished_child_task_ids": unfinished,
                    "all_children_terminal": not unfinished,
                },
            )
            self._append_tool_result(run, tool_result)
        return self._run_loop_protected(self.runs.get(run["id"]) or run)

    def cancel(self, run_id: str, *, cancelled_by: str) -> dict[str, Any]:
        run = self.runs.get(run_id)
        if not run:
            raise KeyError(run_id)
        if run["state"] in {
            AgentRunState.COMPLETED.value,
            AgentRunState.FAILED.value,
            AgentRunState.CANCELLED.value,
        }:
            return run
        return self.runs.transition(
            run_id,
            AgentRunState.CANCELLED,
            event_payload={"cancelled_by": cancelled_by},
        )

    def send_signal(
        self, run_id: str, *, kind: str, message: str | None = None
    ) -> dict[str, Any]:
        """Interrupt a Task that is genuinely in flight instead of letting a
        new message spawn a second, independent Task.

        `kind="abort"` cancels the Task at its next iteration boundary;
        `kind="steer"` injects `message` as a new user turn the Task picks up
        on its next iteration, without pausing it.

        Signals live in their own `task_signals` table, claimed with
        the same atomic immediate-transaction pop used for job leasing
        (`pop_task_signal`) — not on the Task record's own JSON blob.
        That means a signal sent concurrently with `_loop()`'s per-iteration
        `AgentRunStore.update()` can never be silently overwritten before
        it's read: the two writes touch different rows.
        """
        run = self.runs.get(run_id)
        if not run:
            raise KeyError(run_id)
        self.runs.push_signal(
            run_id, run["organization_id"], kind=kind, message=message
        )
        return run

    def _append_tool_result(self, run: dict[str, Any], result: Any) -> None:
        content = json.dumps(
            result.output if result.status == "completed" else {"error": result.error},
            default=str,
        )
        content = frame_untrusted_tool_result(result.tool_name, content)
        self.runs.add_message(
            organization_id=run["organization_id"],
            session_id=run["session_id"],
            task_id=run["id"],
            role="tool",
            content=content,
            tool_call_id=result.call_id,
            name=result.tool_name,
            metadata={"status": result.status, "approval_id": result.approval_id},
        )

    def _toolkit_label(self, definition: Any, call: ModelToolCall) -> str | None:
        if definition and definition.toolkits:
            return definition.toolkits[0]
        if call.name == "COMPOSIO_EXECUTE":
            # The curated linear_*/github_* shortcuts declare a static
            # toolkit on their ToolDefinition; COMPOSIO_EXECUTE covers every
            # other toolkit dynamically via tool_slug, which Composio always
            # names "{TOOLKIT}_{ACTION}" -- good enough for a friendly
            # progress label without an extra composio.describe() round trip.
            slug = str(call.arguments.get("tool_slug") or "")
            if "_" in slug:
                return slug.split("_", 1)[0].lower()
        return None

    def _track_checkpoint(
        self, run: dict[str, Any], call: ModelToolCall, definition: Any
    ) -> None:
        """Phase F: the live Slack status message only updates on a toolkit
        *transition* (e.g. Linear -> GitHub), not on every tool call --
        editing it per call would blow through Slack's rate limits for
        nothing anyone reads. It also only starts firing from the second
        transition onward: a run that never leaves its first toolkit gets
        no status message at all, same as a single-turn no-tool-call reply.
        """
        if (run.get("metadata") or {}).get("suppress_delivery"):
            return
        toolkit = self._toolkit_label(definition, call)
        if not toolkit:
            return
        metadata = dict(run.get("metadata") or {})
        checkpoints = list(metadata.get("status_checkpoints") or [])
        if checkpoints and checkpoints[-1] == toolkit:
            return
        checkpoints.append(toolkit)
        metadata["status_checkpoints"] = checkpoints
        run["metadata"] = metadata
        self.runs.update(run)
        if len(checkpoints) < 2:
            return
        self.enqueue_job(
            organization_id=run["organization_id"],
            kind="delivery.status_update",
            payload={"task_id": run["id"]},
            idempotency_key=f"delivery.status_update:{run['id']}:{len(checkpoints)}",
        )

    def _enqueue_post_run_jobs(self, run: dict[str, Any]) -> None:
        # Children are private specialist evidence. Only a synthesized root
        # conclusion is captured as durable memory or delivered to Slack.
        if run.get("parent_task_id"):
            return
        kinds = ["memory.capture_task", "delivery.send"]
        for kind in kinds:
            self.enqueue_job(
                organization_id=run["organization_id"],
                kind=kind,
                payload={"task_id": run["id"]},
                idempotency_key=f"{kind}:{run['id']}",
            )

    def _slack_conversation(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        """Load previous messages from the exact Slack channel or DM."""
        if run.get("worker_type") in {"general", "pi"} or (run.get("metadata") or {}).get("worker_type") in {"general", "pi"}:
            return []
        slack = (run.get("metadata") or {}).get("slack") or {}
        workspace_id = str(slack.get("workspace_id") or "")
        channel_id = str(
            slack.get("channel_id")
            or (run.get("envelope") or {}).get("destination", {}).get("id")
            or ""
        )
        if not workspace_id or not channel_id:
            return []
        messages = self.runs.store.recent_slack_messages(
            run["organization_id"],
            workspace_id=workspace_id,
            channel_id=channel_id,
            limit=11,
        )
        trigger_event_id = run.get("trigger_event_id")
        return [
            item
            for item in messages
            if item.get("message_id") != trigger_event_id
        ][-10:]

    def _active_skills(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        """Resolve once and pin exact approved skill versions to the run."""
        metadata = dict(run.get("metadata") or {})
        if metadata.get("skills_resolved"):
            return list(metadata.get("active_skills") or [])
        destination = dict(run["envelope"]["destination"])
        if metadata.get("worker_type") in {"general", "pi"}:
            destination = {
                "type": "internal_agent_session",
                "id": run["id"],
                "is_private": True,
            }
        active = self.skills.resolve_for_task(
            organization_id=run["organization_id"],
            task=run["goal"],
            destination=destination,
            trigger_kind=run["trigger_kind"],
            purpose=run["envelope"]["purpose"],
            allowed_scope_ids=run["envelope"]["allowed_scope_ids"],
            available_capabilities=self.tools.available_capabilities(run),
        )
        metadata["skills_resolved"] = True
        metadata["active_skills"] = active
        metadata["skill_refs"] = [
            {
                "id": skill["id"],
                "name": skill.get("name"),
                "version": skill["version"],
                "content_hash": skill.get("content_hash", ""),
                "origin": skill.get("origin") or {},
                "loaded_resources": [],
                "mode": skill["activation_mode"],
                "reason": skill["activation_reason"],
            }
            for skill in active
        ]
        for ref in metadata["skill_refs"]:
            now = utcnow().isoformat()
            self.runs.store.upsert_harness_projection(
                "task_skill_refs",
                {
                    "id": f"{run['id']}:{ref['id']}:{ref['version']}",
                    "organization_id": run["organization_id"],
                    "task_id": run["id"],
                    "status": "active",
                    **ref,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        metadata["skill_index"] = self.skills.index_for_run(
            organization_id=run["organization_id"],
            task=run["goal"],
            allowed_scope_ids=run["envelope"]["allowed_scope_ids"],
            available_capabilities=self.tools.available_capabilities(run),
        )
        metadata["resolved_capabilities"] = sorted(
            self.tools.available_capabilities(run)
        )
        run["metadata"] = metadata
        self.runs.update(run)
        self.audit.record(
            run["organization_id"],
            "agent.skills.resolved",
            {"task_id": run["id"], "skills": metadata["skill_refs"]},
            principal_id=run["envelope"]["principal_id"],
            trace_id=run["trace_id"],
        )
        return active

    def _validate_user_response(
        self,
        *,
        run: dict[str, Any],
        response: str,
        active_skills: list[dict[str, Any]],
    ) -> dict[str, Any]:
        validation = self.skill_validator.validate(
            response=response,
            active_skills=active_skills,
            destination=run["envelope"]["destination"],
        )
        usage = dict(run.get("usage") or {})
        validation_usage = validation.get("usage") or {}
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cost",
        ):
            try:
                usage[key] = float(usage.get(key, 0)) + float(
                    validation_usage.get(key, 0)
                )
            except (TypeError, ValueError):
                continue
        if validation.get("checked"):
            usage["skill_validation_calls"] = int(
                usage.get("skill_validation_calls", 0)
            ) + 1
        run["usage"] = usage
        metadata = dict(run.get("metadata") or {})
        metadata["last_skill_validation"] = {
            key: value
            for key, value in validation.items()
            if key not in {"response", "usage"}
        }
        run["metadata"] = metadata
        self.runs.update(run)
        if validation.get("checked"):
            self.audit.record(
                run["organization_id"],
                "agent.response.skill_validated",
                {
                    "task_id": run["id"],
                    "valid": validation["valid"],
                    "violations": validation["violations"],
                    "skill_refs": validation["skill_refs"],
                    "corrected": validation["response"] != response,
                },
                principal_id=run["envelope"]["principal_id"],
                trace_id=run["trace_id"],
            )
        return validation

    def _deduplicate_tool_calls(
        self, run_id: str, tool_calls: list[ModelToolCall]
    ) -> list[ModelToolCall]:
        """Drop exact-repeat (name, arguments) calls within a single model
        turn — ported from Hermes's ``run_agent.py:_deduplicate_tool_calls``.

        A response occasionally requests the identical call twice (same tool,
        same arguments); executing both burns real tool-call budget and a
        genuine round trip for zero new information, and for a write tool it
        would mean two approval previews for one action. Only the first
        occurrence survives; later ones are dropped before anything —
        execution, turn persistence, or the transcript — ever sees them.
        """
        seen: set[tuple[str, str]] = set()
        unique: list[ModelToolCall] = []
        for call in tool_calls:
            key = (call.name, json.dumps(call.arguments, sort_keys=True, default=str))
            if key in seen:
                log.warning(
                    "Dropped duplicate tool call in the same turn: run_id=%s tool=%s",
                    run_id,
                    call.name,
                )
                continue
            seen.add(key)
            unique.append(call)
        return unique

    @staticmethod
    def _compact_history(messages: list[dict[str, Any]]) -> str:
        """Bounded, auditable transcript compaction without hidden reasoning."""
        lines = []
        for message in messages[-80:]:
            content = " ".join((message.get("content") or "").split())
            if not content:
                continue
            lines.append(f"{message.get('role', 'unknown')}: {content[:500]}")
        summary = "\n".join(lines)
        return summary[-8_000:]
