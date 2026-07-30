"""Durable Hermes-style CompanyAgent model/tool loop."""

from __future__ import annotations

import json
from typing import Any, Callable

from company_brain.domain.models import new_id, utcnow
from company_brain.harness.model_client import ModelClient
from company_brain.harness.models import (
    AgentOutcome,
    AgentRunState,
    InvocationEnvelope,
    ModelToolCall,
    TriggerKind,
)
from company_brain.harness.prompts import build_agent_messages
from company_brain.harness.run_store import AgentRunStore
from company_brain.harness.tools import ToolExecutor


class CompanyAgent:
    def __init__(
        self,
        *,
        runs: AgentRunStore,
        context_service: Any,
        model: ModelClient,
        tools: ToolExecutor,
        audit: Any,
        enqueue_job: Callable[..., dict[str, Any]],
        graph: Any | None = None,
        max_iterations: int = 12,
        max_tool_calls: int = 24,
        context_token_budget: int = 12_000,
        max_total_tokens: int = 100_000,
    ) -> None:
        self.runs = runs
        self.context_service = context_service
        self.model = model
        self.tools = tools
        self.audit = audit
        self.enqueue_job = enqueue_job
        self.graph = graph
        self.max_iterations = max_iterations
        self.max_tool_calls = max_tool_calls
        self.context_token_budget = context_token_budget
        self.max_total_tokens = max_total_tokens

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
        active = self.runs.store.active_run_for_session(session["id"])
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
                    run_id=run["id"],
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
        if initial_message or goal:
            self.runs.add_message(
                organization_id=envelope.organization_id,
                session_id=session["id"],
                run_id=run["id"],
                role="user",
                content=initial_message or goal,
                metadata={"trigger_event_id": trigger_event_id},
            )
        if self.graph:
            self.graph.upsert_entity(
                {
                    "id": run["id"],
                    "organization_id": envelope.organization_id,
                    "entity_type": "agent_run",
                    "name": f"Agent run: {goal[:80]}",
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

    def run(self, run_id: str) -> AgentOutcome:
        run = self.runs.get(run_id)
        if not run:
            raise KeyError(f"run not found: {run_id}")
        if run["state"] == AgentRunState.COMPLETED.value:
            return AgentOutcome(
                run_id=run_id,
                state=AgentRunState.COMPLETED,
                answer=run.get("final_answer"),
                trace_id=run["trace_id"],
            )
        if run["state"] in {
            AgentRunState.WAITING_FOR_APPROVAL.value,
            AgentRunState.WAITING_FOR_HUMAN.value,
        }:
            return AgentOutcome(
                run_id=run_id,
                state=AgentRunState(run["state"]),
                approval_id=run.get("pending_approval_id"),
                trace_id=run["trace_id"],
            )
        if run["state"] == AgentRunState.FAILED.value:
            run = self.runs.transition(run_id, AgentRunState.QUEUED)
        try:
            run = self.runs.transition(
                run_id, AgentRunState.RUNNING, force=run["state"] == "running"
            )
            return self._loop(run)
        except Exception as exc:
            failed = self.runs.fail(run_id, str(exc))
            self.audit.record(
                failed["organization_id"],
                "agent.run.failed",
                {"run_id": run_id, "error": str(exc)},
                principal_id=failed["envelope"]["principal_id"],
                trace_id=failed["trace_id"],
            )
            return AgentOutcome(
                run_id=run_id,
                state=AgentRunState.FAILED,
                error=str(exc),
                trace_id=failed["trace_id"],
            )

    def _loop(self, run: dict[str, Any]) -> AgentOutcome:
        while int(run.get("iteration_count", 0)) < int(run["max_iterations"]):
            persisted = self.runs.get(run["id"])
            if persisted and persisted["state"] == AgentRunState.CANCELLED.value:
                return AgentOutcome(
                    run_id=run["id"],
                    state=AgentRunState.CANCELLED,
                    trace_id=run["trace_id"],
                )
            if int(run.get("tool_call_count", 0)) >= int(run["max_tool_calls"]):
                raise RuntimeError("tool_call_limit")
            if int((run.get("usage") or {}).get("total_tokens", 0)) >= int(
                run.get("max_total_tokens") or self.max_total_tokens
            ):
                raise RuntimeError("model_token_budget")
            if run.get("deadline_at") and utcnow().isoformat() >= run["deadline_at"]:
                raise RuntimeError("worker_deadline_exceeded")
            context = self.context_service.prepare_context(
                organization_id=run["organization_id"],
                task=run["goal"],
                acting_principal_id=run["envelope"]["principal_id"],
                destination=run["envelope"]["destination"],
                purpose=run["envelope"]["purpose"],
                token_budget=self.context_token_budget,
                agent_id="company_agent",
                conversation={"session_id": run["session_id"]},
            ).model_dump(mode="json")
            full_history = self.runs.messages(run["session_id"], limit=200)
            summary = ""
            history = full_history
            if len(full_history) > 50:
                session = self.runs.store.get_harness_record(
                    "agent_sessions", run["session_id"]
                )
                older = full_history[:-30]
                summary = self._compact_history(older)
                history = full_history[-30:]
                if session:
                    session["summary"] = summary
                    session["summary_through_message_id"] = older[-1]["id"]
                    session["updated_at"] = utcnow().isoformat()
                    self.runs.store.upsert_agent_session(session)
            else:
                session = self.runs.store.get_harness_record(
                    "agent_sessions", run["session_id"]
                )
                summary = (session or {}).get("summary") or ""
            messages = build_agent_messages(
                run=run,
                context=context,
                history=history,
                conversation_summary=summary,
            )
            allowed = self.tools.allowed_definitions(run)
            schemas = [definition.openai_schema() for definition in allowed]
            run["iteration_count"] = int(run.get("iteration_count", 0)) + 1
            self.runs.update(run, checkpoint_reason="before_model")
            response = self.model.generate(
                messages=messages,
                tools=schemas,
                temperature=0.1,
                max_tokens=1800,
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
            self.runs.update(run)
            self.runs.append_turn(
                run,
                request={
                    "message_count": len(messages),
                    "context_request_id": context["request_id"],
                    "permission_decision_id": context["permission_decision_id"],
                    "tools": [definition.name for definition in allowed],
                    "model": getattr(self.model, "model", ""),
                },
                response={
                    "content": response.content,
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
                run_id=run["id"],
                role="assistant",
                content=response.content,
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
                answer = response.content.strip()
                if not answer:
                    raise RuntimeError("model returned neither answer nor tool call")
                completed = self.runs.complete(run["id"], answer)
                if self.graph:
                    self.graph.upsert_entity(
                        {
                            "id": completed["id"],
                            "organization_id": completed["organization_id"],
                            "entity_type": "agent_run",
                            "name": f"Agent run: {completed['goal'][:80]}",
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
                    "agent.run.completed",
                    {
                        "run_id": run["id"],
                        "iterations": completed["iteration_count"],
                        "tool_calls": completed["tool_call_count"],
                    },
                    principal_id=run["envelope"]["principal_id"],
                    trace_id=run["trace_id"],
                )
                return AgentOutcome(
                    run_id=run["id"],
                    state=AgentRunState.COMPLETED,
                    answer=answer,
                    trace_id=run["trace_id"],
                )

            for call in response.tool_calls:
                run["tool_call_count"] = int(run.get("tool_call_count", 0)) + 1
                self.runs.update(run)
                result = self.tools.execute(run=run, call=call)
                if (
                    call.name == "request_human_input"
                    and result.status == "completed"
                ):
                    self._append_tool_result(run, result)
                    run["pending_question"] = result.output.get("question")
                    self.runs.update(run)
                    self.runs.transition(
                        run["id"],
                        AgentRunState.WAITING_FOR_HUMAN,
                        event_payload={
                            "question": result.output.get("question"),
                            "reason": result.output.get("reason"),
                        },
                    )
                    self.enqueue_job(
                        organization_id=run["organization_id"],
                        kind="delivery.question",
                        payload={
                            "run_id": run["id"],
                            "question": result.output.get("question"),
                        },
                        idempotency_key=f"delivery.question:{run['id']}:{call.id}",
                    )
                    return AgentOutcome(
                        run_id=run["id"],
                        state=AgentRunState.WAITING_FOR_HUMAN,
                        answer=result.output.get("question"),
                        trace_id=run["trace_id"],
                    )
                if result.status == "waiting_for_approval":
                    run["pending_approval_id"] = result.approval_id
                    run["pending_tool_call_id"] = call.id
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
                        run_id=run["id"],
                        state=AgentRunState.WAITING_FOR_APPROVAL,
                        approval_id=result.approval_id,
                        trace_id=run["trace_id"],
                    )
                self._append_tool_result(run, result)
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
        approval, result = self.tools.resolve_approval(
            approval_id=approval_id,
            approved=approved,
            decided_by=decided_by,
            edited_arguments=edited_arguments,
        )
        run = self.runs.get(approval["run_id"])
        if not run:
            raise KeyError(approval["run_id"])
        run.pop("pending_approval_id", None)
        run.pop("pending_tool_call_id", None)
        self.runs.update(run)
        if result:
            self._append_tool_result(run, result)
        self.runs.transition(run["id"], AgentRunState.RUNNING)
        return self._loop(self.runs.get(run["id"]) or run)

    def resume_with_human_message(
        self, run_id: str, *, message: str, principal_id: str
    ) -> AgentOutcome:
        run = self.runs.get(run_id)
        if not run:
            raise KeyError(run_id)
        self.runs.add_message(
            organization_id=run["organization_id"],
            session_id=run["session_id"],
            run_id=run_id,
            role="user",
            content=message,
            metadata={"principal_id": principal_id, "continuation": True},
        )
        self.runs.transition(run_id, AgentRunState.RUNNING)
        return self._loop(self.runs.get(run_id) or run)

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

    def _append_tool_result(self, run: dict[str, Any], result: Any) -> None:
        content = json.dumps(
            result.output if result.status == "completed" else {"error": result.error},
            default=str,
        )
        self.runs.add_message(
            organization_id=run["organization_id"],
            session_id=run["session_id"],
            run_id=run["id"],
            role="tool",
            content=content,
            tool_call_id=result.call_id,
            name=result.tool_name,
            metadata={"status": result.status, "approval_id": result.approval_id},
        )

    def _enqueue_post_run_jobs(self, run: dict[str, Any]) -> None:
        kinds = ["memory.capture_run"]
        if not (run.get("metadata") or {}).get("suppress_delivery"):
            kinds.append("delivery.send")
        for kind in kinds:
            self.enqueue_job(
                organization_id=run["organization_id"],
                kind=kind,
                payload={"run_id": run["id"]},
                idempotency_key=f"{kind}:{run['id']}",
            )

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
