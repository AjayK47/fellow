"""Durable Hermes-style CompanyAgent model/tool loop."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from docket.domain.models import new_id, utcnow
from docket.harness.model_client import ModelClient
from docket.harness.models import (
    AgentOutcome,
    AgentRunState,
    InvocationEnvelope,
    ModelToolCall,
    TriggerKind,
)
from docket.harness.prompts import build_agent_messages, frame_untrusted_tool_result
from docket.harness.run_store import AgentRunStore
from docket.harness.tools import ToolExecutor

log = logging.getLogger("docket.runtime")


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
        history_compactor: Any | None = None,
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
        self.history_compactor = history_compactor
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
        log.info(
            "Agent run starting: run_id=%s goal=%r trigger=%s principal=%s",
            run_id,
            (run.get("goal") or "")[:120],
            run.get("trigger_kind"),
            run["envelope"]["principal_id"],
        )
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
            if persisted and persisted.get("pending_signal"):
                signal = persisted.pop("pending_signal")
                if signal.get("kind") == "abort":
                    self.runs.update(persisted)
                    cancelled = self.runs.transition(
                        run["id"],
                        AgentRunState.CANCELLED,
                        event_payload={
                            "reason": "steer_abort",
                            "message": signal.get("message"),
                        },
                    )
                    return AgentOutcome(
                        run_id=run["id"],
                        state=AgentRunState.CANCELLED,
                        trace_id=cancelled["trace_id"],
                    )
                if signal.get("kind") == "steer" and signal.get("message"):
                    self.runs.add_message(
                        organization_id=persisted["organization_id"],
                        session_id=persisted["session_id"],
                        run_id=persisted["id"],
                        role="user",
                        content=signal["message"],
                        metadata={"steered": True},
                    )
                self.runs.update(persisted)
                run = persisted
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
                log.info(
                    "Agent run completed: run_id=%s iterations=%s tool_calls=%s answer_len=%s",
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

            # H-08: independent read-only calls run concurrently; anything
            # that can pause the run (writes needing approval, human input)
            # or is otherwise not declared read-safe stays serialized, in
            # its original order, so pause/early-return semantics are never
            # affected by concurrency.
            allowed_by_name = {definition.name: definition for definition in allowed}

            def _is_read_safe(call: ModelToolCall) -> bool:
                if call.name == "request_human_input":
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

            for call in other_calls:
                run["tool_call_count"] = int(run.get("tool_call_count", 0)) + 1
                self.runs.update(run)
                log.info(
                    "Tool call: run_id=%s tool=%s call_id=%s",
                    run["id"],
                    call.name,
                    call.id,
                )
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

    def send_signal(
        self, run_id: str, *, kind: str, message: str | None = None
    ) -> dict[str, Any]:
        """Interrupt a run that is genuinely in flight (RUNNING) instead of
        letting a new message spawn a second, independent run.

        `kind="abort"` cancels the run at its next iteration boundary;
        `kind="steer"` injects `message` as a new user turn the run picks up
        on its next iteration, without pausing it. Checked by `_loop()` at
        the same point it already checks for external cancellation.

        Known limitation: this writes onto the run record's own JSON blob,
        which `_loop()` also periodically overwrites wholesale mid-iteration
        (`AgentRunStore.update()`). A signal landing in the narrow window
        between `_loop()`'s last per-iteration refresh and its next refresh
        can be overwritten before it's ever read. The failure mode is a
        dropped signal requiring a retry, not incorrect execution — a
        dedicated signals table would close this fully if it ever matters
        in practice.
        """
        run = self.runs.get(run_id)
        if not run:
            raise KeyError(run_id)
        run["pending_signal"] = {"kind": kind, "message": message}
        self.runs.update(run)
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
