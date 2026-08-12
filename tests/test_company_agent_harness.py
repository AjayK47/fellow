"""End-to-end tests for the managed Hermes-style company agent harness."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from toda.harness.model_client import ScriptedModelClient
from toda.harness.models import (
    InvocationEnvelope,
    ModelResponse,
    ModelToolCall,
    ToolDefinition,
)


def _envelope(brain, *, conversation: str = "api:test:thread") -> InvocationEnvelope:
    destination = {
        "type": "internal_agent_session",
        "id": "test",
        "is_private": True,
    }
    decision = brain.authz.resolve(
        "org_acme", "person_ajay", destination, "reactive_agent"
    )
    return InvocationEnvelope(
        organization_id="org_acme",
        principal_id="person_ajay",
        conversation_id=conversation,
        destination=destination,
        purpose="reactive_agent",
        permission_decision_id=decision.id,
        allowed_scope_ids=decision.allowed_scope_ids,
    )


def _sign(secret: str, body: bytes, timestamp: str) -> str:
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def test_reactive_run_is_durable_and_captures_memory(brain):
    wake = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="api:test:thread",
        destination={"type": "internal_agent_session", "id": "test", "is_private": True},
        role="user",
        content="What do we know about the Acme export failure?",
        principal_id="person_ajay",
    )
    brain.harness_jobs.enqueue(
        organization_id="org_acme",
        kind="session.dispatch",
        payload={"session_id": wake["session_id"]},
        idempotency_key=f"session.dispatch:{wake['session_id']}",
    )
    drained = brain.harness_worker.drain_until_idle()
    assert drained["failed"] == 0

    run = next(
        task for task in brain.store.list_tasks(
            "org_acme", session_id=wake["session_id"], worker_type="session_manager"
        )
    )
    stored = brain.store.get_task(run["id"])
    assert stored["state"] == "completed"
    assert "permitted company context" in stored["final_answer"]
    assert stored["iteration_count"] == 1
    assert brain.store.list_harness_records(
        "task_turns", "org_acme", where={"task_id": run["id"]}
    )
    checkpoints = brain.store.list_harness_records(
        "checkpoints", "org_acme", where={"task_id": run["id"]}
    )
    assert checkpoints
    assert any(item["state"] == "running" for item in checkpoints)
    captured = [
        source
        for source in brain.store.list_sources("org_acme")
        if source.get("external_id") == run["id"]
        and source.get("source_type") == "agent_task"
    ]
    assert captured, "completed Task must be captured asynchronously"


def test_request_batch_approval_pauses_once_then_pre_approves_declared_actions(brain):
    """A task can front-load its whole plan in one request_batch_approval
    call and get ONE human decision covering every declared action, instead
    of pausing once per gated action it actually takes later."""
    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="api:batch-approval:thread"),
        goal="do several gated things",
        trigger_kind="reactive",
        initial_message="do several gated things",
    )
    run = brain.store.get_task(run["id"])

    batch_call = ModelToolCall(
        id="call_batch",
        name="request_batch_approval",
        arguments={
            "summary": "Set up a recurring check and run some code for it.",
            "actions": [
                {"tool_name": "skill_propose_create", "description": "Draft the recurring check skill"},
                {"tool_name": "run_code", "description": "Compute the summary"},
            ],
        },
    )
    batch_result = brain.tool_executor.execute(run=run, call=batch_call)
    assert batch_result.status == "waiting_for_approval"
    _, resolved = brain.tool_executor.resolve_approval(
        approval_id=batch_result.approval_id, approved=True, decided_by="person_ajay"
    )
    assert resolved.status == "completed"
    run = brain.store.get_task(run["id"])

    # Automation tools are native-allow, so a still-gated tool is needed to
    # prove a declared action consumes exactly one pre-approval unit.
    proposal_call = ModelToolCall(
        id="call_proposal",
        name="skill_propose_create",
        arguments={"name": "redteam", "description": "redteam", "goal": "redteam"},
    )
    result = brain.tool_executor.execute(run=run, call=proposal_call)
    assert result.status != "waiting_for_approval"
    run = brain.store.get_task(run["id"])

    # Only one unit of skill_propose_create was declared -- a second call must
    # be gated normally again, not silently pre-approved forever.
    second_proposal_call = ModelToolCall(
        id="call_proposal_2",
        name="skill_propose_create",
        arguments={"name": "redteam2", "description": "redteam2", "goal": "redteam2"},
    )
    second_result = brain.tool_executor.execute(run=run, call=second_proposal_call)
    assert second_result.status == "waiting_for_approval"

    run_code_call = ModelToolCall(id="call_run_code", name="run_code", arguments={"code": "print(1)"})
    run_code_result = brain.tool_executor.execute(run=run, call=run_code_call)
    assert run_code_result.status != "waiting_for_approval"


def test_batch_approval_does_not_pre_approve_undeclared_tools(brain):
    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="api:batch-approval:undeclared"),
        goal="do one gated thing",
        trigger_kind="reactive",
        initial_message="do one gated thing",
    )
    run = brain.store.get_task(run["id"])
    batch_call = ModelToolCall(
        id="call_batch_narrow",
        name="request_batch_approval",
        arguments={
            "summary": "Just run some code.",
            "actions": [{"tool_name": "run_code", "description": "Compute something"}],
        },
    )
    batch_result = brain.tool_executor.execute(run=run, call=batch_call)
    brain.tool_executor.resolve_approval(
        approval_id=batch_result.approval_id, approved=True, decided_by="person_ajay"
    )
    run = brain.store.get_task(run["id"])
    undeclared_call = ModelToolCall(
        id="call_undeclared_proposal",
        name="skill_propose_create",
        arguments={"name": "redteam3", "description": "redteam3", "goal": "redteam3"},
    )
    result = brain.tool_executor.execute(run=run, call=undeclared_call)
    assert result.status == "waiting_for_approval"


def test_write_tool_pauses_and_resumes_from_approval(brain):
    # A DELETE-shaped Composio action is in the always-gated high-stakes
    # tier (unlike ordinary named connector shortcuts, which now auto-execute
    # under the risk-tiered default) — using it here keeps this test's focus
    # on the generic pause/resume plumbing rather than any specific tool.
    # No Composio API key is configured for this test org, so execution
    # after approval fails locally without ever making a real network call.
    brain.composio.api_key = None
    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ModelToolCall(
                        id="call_delete_repo",
                        name="COMPOSIO_EXECUTE",
                        arguments={
                            "tool_slug": "GITHUB_DELETE_A_REPOSITORY",
                            "arguments": {"owner": "acme", "repo": "exports"},
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ModelResponse(
                content=(
                    "The approved action could not execute because there is no "
                    "connected account for this action; no repository was claimed as deleted."
                )
            ),
        ]
    )
    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="api:approval:thread"),
        goal="Delete the acme/exports repository",
        trigger_kind="reactive",
        initial_message="Delete the acme/exports repository",
    )
    outcome = brain.company_agent.execute_task(run["id"])
    assert outcome.state.value == "waiting_for_approval"
    approval = brain.store.get_harness_record("approvals", outcome.approval_id)
    assert approval["status"] == "pending"
    assert approval["tool_name"] == "COMPOSIO_EXECUTE"

    resumed = brain.company_agent.resume_after_approval(
        approval_id=approval["id"],
        approved=True,
        decided_by="person_ajay",
    )
    assert resumed.state.value == "completed"
    decided = brain.store.get_harness_record("approvals", approval["id"])
    assert decided["status"] == "approved"
    calls = brain.store.list_harness_records(
        "harness_tool_calls", "org_acme", where={"task_id": run["id"]}
    )
    assert calls[0]["status"] == "failed"
    assert "not configured" in calls[0]["error"]


def test_auto_approve_writes_flag_still_creates_an_approved_record(
    brain, monkeypatch
):
    """The dev-only auto_approve_writes flag must skip the human pause, not
    the approval trail itself — a real approval record should still exist,
    just already resolved rather than pending."""
    from toda.harness import tools as tools_module

    monkeypatch.setattr(tools_module.settings, "auto_approve_writes", True)
    # No Composio API key is configured for this test org, so execution
    # after auto-approval fails locally without a real network call.
    brain.composio.api_key = None
    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ModelToolCall(
                        id="call_delete_repo_auto",
                        name="COMPOSIO_EXECUTE",
                        arguments={
                            "tool_slug": "GITHUB_DELETE_A_REPOSITORY",
                            "arguments": {"owner": "acme", "repo": "exports"},
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="Done."),
        ]
    )
    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="api:auto-approve:thread"),
        goal="Delete the acme/exports repository",
        trigger_kind="reactive",
        initial_message="Delete the acme/exports repository",
    )
    outcome = brain.company_agent.execute_task(run["id"])
    assert outcome.state.value == "completed"
    calls = brain.store.list_harness_records(
        "harness_tool_calls", "org_acme", where={"task_id": run["id"]}
    )
    approval = brain.store.get_harness_record("approvals", calls[0]["approval_id"])
    assert approval["status"] == "approved"
    assert approval["decided_by"] == "person_ajay"


def test_slack_send_message_suppresses_automatic_final_answer_delivery(brain, monkeypatch):
    """When the model sends its own (possibly richly-formatted) message via
    slack_send_message, the automatic plain-text final-answer delivery must
    not also fire and double-post once the run completes."""
    monkeypatch.setattr(brain.action_reviewer, "review", lambda **_kwargs: "approve")
    destination = {"type": "slack_channel", "id": "C_TEST", "is_private": False}
    decision = brain.authz.resolve("org_acme", "person_ajay", destination, "reactive_agent")
    envelope = InvocationEnvelope(
        organization_id="org_acme",
        principal_id="person_ajay",
        conversation_id="api:suppress-delivery:thread",
        destination=destination,
        purpose="reactive_agent",
        permission_decision_id=decision.id,
        allowed_scope_ids=decision.allowed_scope_ids,
    )
    run = brain.company_agent.create_invocation(
        envelope=envelope,
        goal="Announce results",
        trigger_kind="reactive",
        initial_message="Announce results",
    )
    run = brain.store.get_task(run["id"])
    assert not (run.get("metadata") or {}).get("suppress_delivery")

    call = ModelToolCall(
        id="call_announce",
        name="slack_send_message",
        arguments={
            "text": "Done.",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Done."}}],
        },
    )
    result = brain.tool_executor.execute(run=run, call=call)
    assert result.status != "waiting_for_approval"

    updated = brain.store.get_task(run["id"])
    assert updated["metadata"]["suppress_delivery"] is True


def test_resolving_an_approval_updates_the_original_slack_message(brain):
    """The Approve/Deny buttons must not be left live forever on a message
    whose approval has already been decided — resolving it should edit the
    original Slack message in place, regardless of whether the decision
    came from a Slack button click or the dashboard."""
    from toda.domain.models import utcnow

    now = utcnow().isoformat()
    approval_id = "approval_slack_update_test"
    brain.store.upsert_approval(
        {
            "id": approval_id,
            "organization_id": "org_acme",
            "task_id": "task_does_not_need_to_exist",
            "tool_call_id": "call_test",
            "status": "pending",
            "created_at": now,
            "updated_at": now,
        }
    )
    brain.store.upsert_delivery(
        {
            "id": "delivery_slack_update_test",
            "organization_id": "org_acme",
            "task_id": "task_does_not_need_to_exist",
            "kind": "approval_request",
            "status": "completed",
            "idempotency_key": f"slack.approval:{approval_id}",
            "provider_result": {"ok": True, "channel": "C_TEST", "ts": "111.222"},
            "created_at": now,
            "updated_at": now,
        }
    )

    calls: list[dict] = []
    brain.delivery.slack.update_message = lambda **kwargs: calls.append(kwargs)

    brain.delivery.mark_approval_decided(
        approval_id, approved=True, decided_by="person_ajay"
    )

    assert len(calls) == 1
    assert calls[0]["channel_id"] == "C_TEST"
    assert calls[0]["timestamp"] == "111.222"
    assert "Approved" in calls[0]["text"]
    assert "person_ajay" in calls[0]["text"]
    assert calls[0]["blocks"][0]["text"]["text"] == "*Approved* by person_ajay"


def test_approval_card_truncates_a_large_argument_preview_instead_of_failing(brain):
    """Slack rejects an entire postMessage call (invalid_blocks) if any
    section's text.text exceeds 3000 characters -- a tool call whose
    arguments embed a large text field (a full report/document body, e.g.
    pdf_create) must still produce a postable approval card instead of
    silently dead-lettering with the human never seeing an approval
    prompt at all."""
    from toda.domain.models import utcnow

    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="api:huge-approval:thread"),
        goal="Create a PDF report",
        trigger_kind="reactive",
        initial_message="Create a PDF report",
    )
    now = utcnow().isoformat()
    approval_id = "approval_huge_preview_test"
    huge_text = "A" * 10_000
    brain.store.upsert_approval(
        {
            "id": approval_id,
            "organization_id": "org_acme",
            "task_id": run["id"],
            "tool_call_id": "call_huge_pdf",
            "status": "pending",
            "requested_by": "person_ajay",
            "allowed_approver_ids": ["person_ajay"],
            "preview": {
                "action": "pdf_create",
                "description": "Typed artifact-only pdf create operation.",
                "arguments": {"title": "Report", "text": huge_text},
            },
            "created_at": now,
            "updated_at": now,
        }
    )

    sent: list[dict] = []
    brain.delivery.slack.send = lambda **kwargs: (sent.append(kwargs), {"ok": True, "channel": "C_TEST", "ts": "1.1"})[1]

    result = brain.delivery.deliver_approval(approval_id)

    assert result["delivery"]["status"] == "completed"
    assert len(sent) == 1
    section_text = sent[0]["blocks"][0]["text"]["text"]
    assert len(section_text) <= 3000
    assert "truncated" in section_text
    # The Approve/Deny buttons must still be present and untouched.
    actions = sent[0]["blocks"][1]
    assert [el["action_id"] for el in actions["elements"]] == [
        "toda_approve",
        "toda_deny",
    ]


def test_resume_after_approval_failure_does_not_leave_a_zombie_run(brain):
    """run() has always wrapped _loop() in a try/except that marks the run
    FAILED on any exception. resume_after_approval() called _loop()
    directly with no such protection — a crash right after a human
    approved a write (a model API error, anything) left the run stuck at
    RUNNING forever: no failure recorded, nothing to retry, no answer or
    error ever surfacing to the human who just approved it."""
    # No Composio API key is configured for this test org, so the tool call
    # made on resume fails locally without a real network call.
    brain.composio.api_key = None

    class CrashesOnResume:
        model = "crashes-on-resume"

        def __init__(self):
            self.calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    content="",
                    tool_calls=[
                        ModelToolCall(
                            id="call_zombie",
                            name="COMPOSIO_EXECUTE",
                            arguments={
                                "tool_slug": "GITHUB_DELETE_A_REPOSITORY",
                                "arguments": {"owner": "acme", "repo": "zombie"},
                            },
                        )
                    ],
                    finish_reason="tool_calls",
                )
            raise RuntimeError(
                "OpenRouter 400 for model crashes-on-resume: context length exceeded"
            )

        def context_length(self):
            return 128_000

    brain.company_agent.model = CrashesOnResume()
    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="api:zombie:thread"),
        goal="Delete the acme/zombie repository",
        trigger_kind="reactive",
        initial_message="Delete the acme/zombie repository",
    )
    outcome = brain.company_agent.execute_task(run["id"])
    assert outcome.state.value == "waiting_for_approval"

    resumed = brain.company_agent.resume_after_approval(
        approval_id=outcome.approval_id,
        approved=True,
        decided_by="person_ajay",
    )
    assert resumed.state.value == "failed"
    assert "context length" in resumed.error

    persisted = brain.store.get_task(run["id"])
    assert persisted["state"] == "failed"


def test_a_non_retryable_run_failure_notifies_slack_instead_of_going_silent(brain):
    """A run failing closed (no hang, no partial write) is not the same as
    the human waiting on it in Slack ever hearing about it -- before this,
    a non-retryable failure (e.g. malformed model tool-call arguments) left
    the run "failed" in the database with nothing ever delivered, which is
    indistinguishable from a hang from the human's side."""

    class RaisesNonRetryableError:
        model = "raises-non-retryable"

        def generate(self, **_kwargs):
            raise RuntimeError(
                "invalid model tool arguments: Unterminated string starting "
                "at: line 1 column 7039 (char 7038)"
            )

        def context_length(self):
            return 128_000

    brain.company_agent.model = RaisesNonRetryableError()

    routed = brain.event_router.route(
        _reactive_event_payload(
            event_id="ev_notify_failure",
            conversation_key="slack:TACME:Cfail:1",
            text="Research computer-use SaaS options and write a PDF report",
            channel_id="Cfail",
        )
    )
    assert routed["route"] == "session_wake"

    drained = brain.harness_worker.drain_until_idle()
    assert drained["failed"] == 0, drained  # the job itself is not retried

    task_id = next(
        r["result"]["task_id"] for r in drained["results"] if r["kind"] == "session.dispatch"
    )
    persisted = brain.store.get_task(task_id)
    assert persisted["state"] == "failed"

    deliveries = brain.store.list_harness_records(
        "deliveries", "org_acme", where={"task_id": task_id}
    )
    failures = [d for d in deliveries if d.get("kind") == "task_failure"]
    assert len(failures) == 1
    assert "couldn't finish" in failures[0]["text"].lower()
    assert "unterminated string" in failures[0]["text"].lower()


def test_a_retryable_run_failure_does_not_notify_slack_before_retrying(brain):
    """A transient error (rate limit, timeout, ...) is retried by the queue
    and commonly recovers silently -- notifying Slack the first time it
    fails would be a false alarm, not useful signal."""

    class FlakyThenFails:
        model = "flaky-then-fails"

        def generate(self, **_kwargs):
            raise RuntimeError("connection reset by peer")

        def context_length(self):
            return 128_000

    brain.company_agent.model = FlakyThenFails()

    routed = brain.event_router.route(
        _reactive_event_payload(
            event_id="ev_retryable_failure",
            conversation_key="slack:TACME:Cretry:1",
            text="Check the export pipeline status",
            channel_id="Cretry",
        )
    )
    assert routed["route"] == "session_wake"

    drained = brain.harness_worker.drain_once()
    assert drained["failed"] == 1  # the session.dispatch job itself is re-queued to retry

    # The failure marks the Task itself terminal immediately (no partial
    # state left running) even though the *job* retries -- retryability is
    # about whether the Session gets woken again automatically and whether
    # Slack is notified, not about the Task's own state.
    tasks = brain.store.list_tasks(
        "org_acme", session_id=routed["session_id"], worker_type="session_manager"
    )
    assert len(tasks) == 1
    assert tasks[0]["state"] == "failed"

    deliveries = brain.store.list_harness_records(
        "deliveries", "org_acme", where={"task_id": tasks[0]["id"]}
    )
    assert not [d for d in deliveries if d.get("kind") == "run_failure"]

    # And the Session was left `queued`, not idle -- a later dispatch will
    # naturally retry it, matching the old job-retry behavior's intent.
    session = brain.sessions.get(routed["session_id"])
    assert session["manager_state"] == "queued"


def test_parallel_read_tool_calls_execute_concurrently(brain):
    """H-08: independent read-risk tool calls proposed in one iteration must
    run concurrently, not one after another."""
    from toda.harness.models import ToolDefinition

    call_log: list[tuple[str, float]] = []

    def _slow_read(args: dict, _runtime: dict) -> dict:
        call_log.append((f"start:{args['label']}", time.monotonic()))
        time.sleep(0.3)
        call_log.append((f"end:{args['label']}", time.monotonic()))
        return {"label": args["label"]}

    brain.tool_executor.registry.register(
        ToolDefinition(
            name="slow_read_test_tool",
            description="test-only slow read tool",
            input_schema={
                "type": "object",
                "properties": {"label": {"type": "string"}},
                "required": ["label"],
                "additionalProperties": False,
            },
            risk="read",
        ),
        _slow_read,
    )

    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ModelToolCall(
                        id="call_a", name="slow_read_test_tool", arguments={"label": "a"}
                    ),
                    ModelToolCall(
                        id="call_b", name="slow_read_test_tool", arguments={"label": "b"}
                    ),
                ],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="Both lookups done."),
        ]
    )
    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="api:parallel:thread"),
        goal="run two slow reads",
        trigger_kind="reactive",
        initial_message="run two slow reads",
    )
    started = time.monotonic()
    outcome = brain.company_agent.execute_task(run["id"])
    elapsed = time.monotonic() - started

    assert outcome.state.value == "completed"
    # Serial execution would take >=0.6s; concurrent execution should finish
    # in roughly one call's duration plus overhead.
    assert elapsed < 0.55, f"expected concurrent execution, took {elapsed:.2f}s"
    labels_started = {name.split(":")[1] for name, _ in call_log if name.startswith("start")}
    assert labels_started == {"a", "b"}

    calls = brain.store.list_harness_records(
        "harness_tool_calls", "org_acme", where={"task_id": run["id"]}
    )
    assert len(calls) == 2
    assert all(call["status"] == "completed" for call in calls)


def _spy_on_prepare_context(brain) -> list[int]:
    """Wrap context_service.prepare_context to count real invocations,
    without changing its behavior."""
    calls: list[int] = []
    original = brain.context.prepare_context

    def _wrapped(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    brain.context.prepare_context = _wrapped
    return calls


def test_context_is_not_recomputed_across_read_only_iterations(brain):
    """prepare_context() (embeddings included) used to run unconditionally
    on every loop iteration -- expensive and unnecessary when nothing in
    that iteration could have changed what it would return. A run whose
    only tool call is read-risk should only pay for it once."""
    calls = _spy_on_prepare_context(brain)
    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ModelToolCall(id="call_read", name="brain_search", arguments={"query": "Acme"})
                ],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="Done reading."),
        ]
    )
    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="api:context-cache:read-only"),
        goal="look something up",
        trigger_kind="reactive",
        initial_message="look something up",
    )
    outcome = brain.company_agent.execute_task(run["id"])
    assert outcome.state.value == "completed"
    assert len(calls) == 1


def test_context_is_recomputed_after_a_write_tool_call(brain, monkeypatch):
    """A write-risk tool call can change facts/memories the model should
    see on its very next turn -- the cache must invalidate in that case,
    not just blindly reuse the first snapshot for the whole run."""
    monkeypatch.setattr(brain.action_reviewer, "review", lambda **_kwargs: "approve")
    calls = _spy_on_prepare_context(brain)
    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ModelToolCall(
                        id="call_write",
                        name="workspace_write",
                        arguments={"path": "notes.txt", "content": "hello"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="Wrote the file."),
        ]
    )
    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="api:context-cache:write"),
        goal="write a file",
        trigger_kind="reactive",
        initial_message="write a file",
    )
    outcome = brain.company_agent.execute_task(run["id"])
    assert outcome.state.value == "completed"
    assert len(calls) == 2


def test_read_and_write_calls_in_one_iteration_still_pause_correctly(brain):
    """A read call alongside a write call in the same model turn must still
    run the read, then correctly pause on the write for approval — mixing
    risk levels in one iteration must not break pause/resume semantics."""
    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ModelToolCall(
                        id="call_read", name="brain_search", arguments={"query": "Acme export"}
                    ),
                    ModelToolCall(
                        id="call_write",
                        name="COMPOSIO_EXECUTE",
                        arguments={
                            "tool_slug": "GITHUB_DELETE_A_REPOSITORY",
                            "arguments": {"owner": "acme", "repo": "exports"},
                        },
                    ),
                ],
                finish_reason="tool_calls",
            ),
        ]
    )
    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="api:mixed-risk:thread"),
        goal="Look up context then delete the repository",
        trigger_kind="reactive",
        initial_message="Look up context then delete the repository",
    )
    outcome = brain.company_agent.execute_task(run["id"])
    assert outcome.state.value == "waiting_for_approval"

    messages = brain.store.list_session_messages(run["session_id"])
    tool_messages = [m for m in messages if m["role"] == "tool"]
    assert any(m.get("tool_call_id") == "call_read" for m in tool_messages)
    assert not any(m.get("tool_call_id") == "call_write" for m in tool_messages)


def test_duplicate_tool_calls_in_one_turn_execute_only_once(brain):
    """A single model turn requesting the identical (name, arguments) call
    twice must only execute it once -- the repeat is dropped before
    execution, turn persistence, or the transcript ever see it."""
    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ModelToolCall(
                        id="call_a", name="brain_search", arguments={"query": "Acme export"}
                    ),
                    ModelToolCall(
                        id="call_b", name="brain_search", arguments={"query": "Acme export"}
                    ),
                ],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="Found the relevant context.", finish_reason="stop"),
        ]
    )
    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="api:dup-tool-call:thread"),
        goal="Look up export context",
        trigger_kind="reactive",
        initial_message="Look up export context",
    )
    outcome = brain.company_agent.execute_task(run["id"])
    assert outcome.state.value == "completed"

    completed = brain.store.get_task(run["id"])
    assert completed["tool_call_count"] == 1

    calls = brain.store.list_harness_records(
        "harness_tool_calls", "org_acme", where={"task_id": run["id"]}
    )
    assert len(calls) == 1

    messages = brain.store.list_session_messages(run["session_id"])
    tool_messages = [m for m in messages if m["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_a"


def _reactive_event_payload(
    *, event_id: str, conversation_key: str, text: str, channel_id: str = "Csteer"
) -> dict:
    return {
        "id": event_id,
        "organization_id": "org_acme",
        "source": "slack",
        "type": "slack.channel_message",
        "actor": {"id": "person_ajay", "type": "person"},
        "scope": {
            "workspace_id": "TACME",
            "channel_id": channel_id,
            "visibility": "organization_public",
            "scope_id": "scope_org_public",
        },
        "payload": {"text": text, "conversation_key": conversation_key},
        "dedupe_key": f"dedupe-{event_id}",
        "is_direct_agent_request": True,
    }


def test_steer_signal_is_injected_before_the_next_model_call(brain):
    model = ScriptedModelClient([ModelResponse(content="Updated plan noted.")])
    brain.company_agent.model = model
    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="api:steer-pickup:thread"),
        goal="Investigate exports",
        trigger_kind="reactive",
        initial_message="Investigate exports",
    )
    brain.company_agent.send_signal(run["id"], kind="steer", message="Focus on Q3 only")
    outcome = brain.company_agent.execute_task(run["id"])
    assert outcome.state.value == "completed"

    messages = brain.store.list_session_messages(run["session_id"])
    steered = [m for m in messages if (m.get("metadata") or {}).get("steered")]
    assert steered and steered[0]["content"] == "Focus on Q3 only"
    # The steer message must have reached the model that produced the answer.
    request = model.requests[0]
    assert any(m.get("content") == "Focus on Q3 only" for m in request["messages"])

    assert brain.store.pop_task_signal(run["id"]) is None


def test_abort_signal_cancels_the_run_before_any_model_call(brain):
    model = ScriptedModelClient([ModelResponse(content="should never be reached")])
    brain.company_agent.model = model
    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="api:abort-pickup:thread"),
        goal="Investigate exports",
        trigger_kind="reactive",
        initial_message="Investigate exports",
    )
    brain.company_agent.send_signal(run["id"], kind="abort", message="stop")
    outcome = brain.company_agent.execute_task(run["id"])
    assert outcome.state.value == "cancelled"
    assert model.requests == []


def _pending_approval(brain, *, conversation: str) -> str:
    # No Composio API key is configured for this test org, so execution
    # after approval fails locally without a real network call.
    brain.composio.api_key = None
    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ModelToolCall(
                        id="call_delete_repo",
                        name="COMPOSIO_EXECUTE",
                        arguments={
                            "tool_slug": "GITHUB_DELETE_A_REPOSITORY",
                            "arguments": {"owner": "acme", "repo": "exports"},
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="Handled via Slack interaction."),
        ]
    )
    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation=conversation),
        goal="Delete the acme/exports repository",
        trigger_kind="reactive",
        initial_message="Delete the acme/exports repository",
    )
    outcome = brain.company_agent.execute_task(run["id"])
    assert outcome.state.value == "waiting_for_approval"
    return outcome.approval_id


def _interaction_payload(*, action_id: str, approval_id: str, user_id: str) -> bytes:
    return json.dumps(
        {
            "type": "block_actions",
            "user": {"id": user_id},
            "team": {"id": "T_ACME"},
            "actions": [{"action_id": action_id, "value": approval_id}],
        }
    ).encode()


def test_socket_mode_interaction_resolves_approval(brain):
    """H-01: approval buttons must work over the same unsigned Socket Mode
    path used for events, not only the signed HTTP interactions route."""
    approval_id = _pending_approval(brain, conversation="api:socket-approval:thread")
    brain.store.upsert_simple_harness_record(
        "identity_mappings",
        {
            "id": "identity_test_socket",
            "organization_id": "org_acme",
            "source": "slack",
            "source_user_id": "U_APPROVER",
            "principal_id": "person_ajay",
        },
    )

    result = brain.slack_gateway.accept_interaction(
        body=_interaction_payload(
            action_id="toda_approve",
            approval_id=approval_id,
            user_id="U_APPROVER",
        ),
        allow_unsigned=True,
    )
    assert result["accepted"] is True

    drained = brain.harness_worker.drain_until_idle()
    assert drained["failed"] == 0
    decided = brain.store.get_harness_record("approvals", approval_id)
    assert decided["status"] == "approved"
    assert decided["decided_by"] == "person_ajay"

    # A duplicate click (Slack retries, or a slow double-tap) must not error
    # or attempt to re-resolve an already-decided approval.
    duplicate = brain.slack_gateway.accept_interaction(
        body=_interaction_payload(
            action_id="toda_approve",
            approval_id=approval_id,
            user_id="U_APPROVER",
        ),
        allow_unsigned=True,
    )
    assert duplicate["accepted"] is True
    redrained = brain.harness_worker.drain_until_idle()
    assert redrained["failed"] == 0
    still_decided = brain.store.get_harness_record("approvals", approval_id)
    assert still_decided["status"] == "approved"


def test_socket_mode_interaction_rejects_unauthorized_approver(brain):
    from toda.harness.slack import UnauthorizedApprover

    approval_id = _pending_approval(brain, conversation="api:socket-approval:unauth")
    # No identity mapping is registered for this Slack user, so it resolves
    # to "slack:U_STRANGER" — not the requester ("person_ajay") and not in
    # allowed_approver_ids.
    try:
        brain.slack_gateway.accept_interaction(
            body=_interaction_payload(
                action_id="toda_approve",
                approval_id=approval_id,
                user_id="U_STRANGER",
            ),
            allow_unsigned=True,
        )
        assert False, "expected UnauthorizedApprover"
    except UnauthorizedApprover:
        pass

    still_pending = brain.store.get_harness_record("approvals", approval_id)
    assert still_pending["status"] == "pending"


def _pending_question_with_choices(brain, *, conversation: str) -> tuple[str, list[str]]:
    choices = ["Rollback the deploy", "Wait and monitor", "Page on-call"]
    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ModelToolCall(
                        id="call_ask",
                        name="request_human_input",
                        arguments={
                            "question": "How should we handle the export failure?",
                            "reason": "Multiple valid remediation paths.",
                            "choices": choices,
                        },
                    )
                ],
                finish_reason="tool_calls",
            )
        ]
    )
    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation=conversation),
        goal="Handle the export failure",
        trigger_kind="reactive",
        initial_message="Handle the export failure",
    )
    outcome = brain.company_agent.execute_task(run["id"])
    assert outcome.state.value == "waiting_for_human"
    return run["id"], choices


def _choice_interaction_payload(
    *, action_id: str, task_id: str, index: int, user_id: str, is_select: bool = False
) -> bytes:
    value = f"{task_id}::{index}"
    action = (
        {"type": "static_select", "action_id": action_id, "selected_option": {"value": value}}
        if is_select
        else {"type": "button", "action_id": action_id, "value": value}
    )
    return json.dumps(
        {
            "type": "block_actions",
            "user": {"id": user_id},
            "team": {"id": "T_ACME"},
            "actions": [action],
        }
    ).encode()


def test_choice_button_click_resumes_run_with_selected_text(brain):
    task_id, choices = _pending_question_with_choices(brain, conversation="api:choice-button:thread")
    brain.company_agent.model = ScriptedModelClient([ModelResponse(content="Rolling back now.")])
    brain.store.upsert_simple_harness_record(
        "identity_mappings",
        {
            "id": "identity_choice_1",
            "organization_id": "org_acme",
            "source": "slack",
            "source_user_id": "U_CHOOSER",
            "principal_id": "person_ajay",
        },
    )

    result = brain.slack_gateway.accept_interaction(
        body=_choice_interaction_payload(
            action_id="toda_choice", task_id=task_id, index=0, user_id="U_CHOOSER"
        ),
        allow_unsigned=True,
    )
    assert result["accepted"] is True

    drained = brain.harness_worker.drain_until_idle()
    assert drained["failed"] == 0
    stored = brain.store.get_task(task_id)
    assert stored["state"] == "completed"
    messages = brain.store.list_session_messages(stored["session_id"], limit=50)
    assert any(m["content"] == choices[0] and m["role"] == "user" for m in messages)


def test_choice_select_click_resumes_run_with_selected_text(brain):
    """The same task_id::index encoding resolves identically whether Slack
    rendered buttons or a static_select — the resolver doesn't care which
    UI produced the click."""
    task_id, choices = _pending_question_with_choices(brain, conversation="api:choice-select:thread")
    brain.company_agent.model = ScriptedModelClient([ModelResponse(content="Monitoring now.")])
    brain.store.upsert_simple_harness_record(
        "identity_mappings",
        {
            "id": "identity_choice_2",
            "organization_id": "org_acme",
            "source": "slack",
            "source_user_id": "U_CHOOSER2",
            "principal_id": "person_ajay",
        },
    )

    result = brain.slack_gateway.accept_interaction(
        body=_choice_interaction_payload(
            action_id="toda_choice_select",
            task_id=task_id,
            index=1,
            user_id="U_CHOOSER2",
            is_select=True,
        ),
        allow_unsigned=True,
    )
    assert result["accepted"] is True
    drained = brain.harness_worker.drain_until_idle()
    assert drained["failed"] == 0
    messages = brain.store.list_session_messages(brain.store.get_task(task_id)["session_id"], limit=50)
    assert any(m["content"] == choices[1] and m["role"] == "user" for m in messages)


def test_choice_button_click_resumes_session_manager_task(brain):
    choices = ["Rollback the deploy", "Wait and monitor", "Page on-call"]
    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ModelToolCall(
                        id="call_task_ask",
                        name="request_human_input",
                        arguments={
                            "question": "How should we handle the export failure?",
                            "reason": "Multiple valid remediation paths.",
                            "choices": choices,
                        },
                    )
                ],
                finish_reason="tool_calls",
            )
        ]
    )
    wake = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="slack:T_ACME:D_CHOICE_TASK",
        destination={"type": "dm", "id": "D_CHOICE_TASK", "is_private": True},
        role="user",
        content="Handle the export failure",
        principal_id="person_ajay",
    )
    waiting = brain.session_manager.dispatch(wake["session_id"])
    assert waiting is not None
    assert waiting.state.value == "waiting_for_human"

    brain.company_agent.model = ScriptedModelClient(
        [ModelResponse(content="Rolling back now.")]
    )
    brain.store.upsert_simple_harness_record(
        "identity_mappings",
        {
            "id": "identity_choice_task",
            "organization_id": "org_acme",
            "source": "slack",
            "source_user_id": "U_TASK_CHOOSER",
            "principal_id": "person_ajay",
        },
    )
    result = brain.slack_gateway.accept_interaction(
        body=_choice_interaction_payload(
            action_id="toda_choice",
            task_id=waiting.task_id,
            index=0,
            user_id="U_TASK_CHOOSER",
        ),
        allow_unsigned=True,
    )
    assert result["accepted"] is True
    assert brain.harness_worker.drain_until_idle()["failed"] == 0
    stored = brain.store.get_task(waiting.task_id)
    assert stored["state"] == "completed"
    messages = brain.store.list_session_messages(wake["session_id"], limit=50)
    assert any(
        message["content"] == choices[0] and message["role"] == "user"
        for message in messages
    )


def test_choice_interaction_on_already_resumed_run_is_a_noop(brain):
    task_id, choices = _pending_question_with_choices(brain, conversation="api:choice-stale:thread")
    brain.company_agent.model = ScriptedModelClient([ModelResponse(content="Done.")])
    first = brain.slack_gateway.accept_interaction(
        body=_choice_interaction_payload(
            action_id="toda_choice", task_id=task_id, index=0, user_id="U_FIRST"
        ),
        allow_unsigned=True,
    )
    assert first["accepted"] is True
    brain.harness_worker.drain_until_idle()
    assert brain.store.get_task(task_id)["state"] == "completed"

    # A second, stale click (different choice, e.g. a slow double-tap) must
    # not raise or attempt to resume a run that has already finished.
    second = brain.slack_gateway.accept_interaction(
        body=_choice_interaction_payload(
            action_id="toda_choice", task_id=task_id, index=1, user_id="U_SECOND"
        ),
        allow_unsigned=True,
    )
    assert second["accepted"] is False


def test_deliver_question_renders_buttons_or_select_by_choice_count(brain):
    task_id, choices = _pending_question_with_choices(brain, conversation="api:choice-render:thread")

    few = brain.delivery.deliver_question(task_id, "Pick one", choices=choices)
    few_blocks = few["delivery"]["provider_result"]["blocks"]
    assert any(b["type"] == "actions" for b in few_blocks)

    many = [f"Option {i}" for i in range(8)]
    result = brain.delivery.deliver_question(task_id, "Pick one of many", choices=many)
    many_blocks = result["delivery"]["provider_result"]["blocks"]
    assert many_blocks[0]["accessory"]["type"] == "static_select"
    assert len(many_blocks[0]["accessory"]["options"]) == 8

    plain = brain.delivery.deliver_question(task_id, "No choices here")
    assert plain["delivery"]["provider_result"]["blocks"] == []


def test_status_checkpoint_tracking_and_live_update_message(brain):
    """Phase F: the live Slack status message only starts once a run
    crosses a *second*, distinct toolkit -- a single-toolkit run gets no
    status message at all -- and every checkpoint after that edits the
    same message in place instead of posting a new one."""
    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="status-checkpoints"),
        goal="Cross-toolkit task",
        trigger_kind="reactive",
        initial_message="Cross-toolkit task",
    )
    linear_call = ModelToolCall(
        id="c1",
        name="COMPOSIO_EXECUTE",
        arguments={"tool_slug": "LINEAR_CREATE_ISSUE", "arguments": {}},
    )
    linear_definition = ToolDefinition(
        name="COMPOSIO_EXECUTE",
        description="d",
        input_schema={"type": "object"},
    )
    github_call = ModelToolCall(
        id="c2",
        name="COMPOSIO_EXECUTE",
        arguments={"tool_slug": "GITHUB_CREATE_ISSUE", "arguments": {}},
    )
    github_definition = ToolDefinition(
        name="COMPOSIO_EXECUTE",
        description="d",
        input_schema={"type": "object"},
    )

    brain.company_agent._track_checkpoint(run, linear_call, linear_definition)
    jobs = brain.harness_jobs.lease(
        worker_id="test-f-1", limit=10, kinds=["delivery.status_update"]
    )
    assert jobs == []  # a single toolkit alone never triggers a status message

    brain.company_agent._track_checkpoint(run, github_call, github_definition)
    jobs = brain.harness_jobs.lease(
        worker_id="test-f-2", limit=10, kinds=["delivery.status_update"]
    )
    assert len(jobs) == 1
    assert jobs[0]["payload"]["task_id"] == run["id"]

    refreshed = brain.store.get_task(run["id"])
    assert refreshed["metadata"]["status_checkpoints"] == ["linear", "github"]

    sent: list[dict] = []
    brain.delivery.slack.send = lambda **kwargs: (
        sent.append(kwargs),
        {"ok": True, "channel": "C_STATUS", "ts": "100.1"},
    )[1]
    result = brain.delivery.deliver_status_update(run["id"])
    assert result["delivered"] is True
    assert len(sent) == 1
    assert "linear" in sent[0]["blocks"][0]["text"]["text"]
    assert "github" in sent[0]["blocks"][0]["text"]["text"]

    # A third checkpoint must EDIT that same message, not post a new one.
    slack_call = ModelToolCall(
        id="c3",
        name="COMPOSIO_EXECUTE",
        arguments={"tool_slug": "SLACK_SEND_MESSAGE"},
    )
    slack_definition = ToolDefinition(
        name="COMPOSIO_EXECUTE", description="d", input_schema={"type": "object"}
    )
    brain.company_agent._track_checkpoint(
        brain.store.get_task(run["id"]), slack_call, slack_definition
    )

    updated: list[dict] = []
    brain.delivery.slack.update_message = lambda **kwargs: (
        updated.append(kwargs),
        {"ok": True, "channel": "C_STATUS", "ts": "100.1"},
    )[1]
    brain.delivery.deliver_status_update(run["id"])
    assert len(sent) == 1  # still just the one original post
    assert len(updated) == 1
    assert updated[0]["channel_id"] == "C_STATUS"
    assert updated[0]["timestamp"] == "100.1"
    assert "slack" in updated[0]["text"]


def test_signed_slack_mention_routes_through_full_harness(brain):
    secret = "test-signing-secret"
    brain.slack_gateway.config.slack_signing_secret = secret
    brain.slack_gateway.config.slack_bot_user_id = "UBOT"
    brain.store.upsert_simple_harness_record(
        "identity_mappings",
        {
            "id": "identity_slack_ajay",
            "organization_id": "org_acme",
            "source": "slack",
            "source_user_id": "UAJAY",
            "principal_id": "person_ajay",
        },
    )
    payload = {
        "type": "event_callback",
        "team_id": "TACME",
        "event_id": "Ev100",
        "event": {
            "type": "app_mention",
            "user": "UAJAY",
            "channel": "CGENERAL",
            "channel_type": "channel",
            "text": "<@UBOT> who owns the export worker?",
            "ts": "100.001",
        },
    }
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    accepted = brain.slack_gateway.accept(
        body=body,
        headers={
            "x-slack-request-timestamp": timestamp,
            "x-slack-signature": _sign(secret, body, timestamp),
        },
    )
    assert accepted["accepted"] is True
    drained = brain.harness_worker.drain_until_idle()
    assert drained["failed"] == 0
    tasks = brain.store.list_tasks("org_acme", worker_type="session_manager")
    run = next(item for item in tasks if item.get("trigger_event_id") == "Ev100")
    assert run["state"] == "completed"
    assert run["envelope"]["destination"]["id"] == "CGENERAL"
    assert any(
        source.get("external_id") == "Ev100"
        for source in brain.store.list_sources("org_acme")
    )


def test_message_and_app_mention_callbacks_for_one_slack_message_are_deduped(brain):
    brain.slack_gateway.config.slack_bot_user_id = "UBOT"
    common_event = {
        "user": "UAJAY",
        "channel": "CGENERAL",
        "text": "Hey <@UBOT>",
        "ts": "125.001",
        "client_msg_id": "client-message-one",
    }
    message_payload = {
        "type": "event_callback",
        "team_id": "TACME",
        "event_id": "EvMessageVariant",
        "event": {
            **common_event,
            "type": "message",
            "channel_type": "channel",
        },
    }
    mention_payload = {
        "type": "event_callback",
        "team_id": "TACME",
        "event_id": "EvMentionVariant",
        "event": {**common_event, "type": "app_mention"},
    }

    first = brain.slack_gateway.accept(
        body=json.dumps(message_payload).encode(), headers={}, allow_unsigned=True
    )
    second = brain.slack_gateway.accept(
        body=json.dumps(mention_payload).encode(), headers={}, allow_unsigned=True
    )

    assert first["accepted"] is True
    assert second == {
        "accepted": False,
        "duplicate": True,
        "event_id": "EvMentionVariant",
    }
    drained = brain.harness_worker.drain_until_idle()
    assert drained["failed"] == 0
    matching_runs = [
        run
        for run in brain.store.list_tasks("org_acme", worker_type="session_manager")
        if run.get("trigger_event_id") in {"EvMessageVariant", "EvMentionVariant"}
    ]
    assert len(matching_runs) == 1
    deliveries = brain.store.list_harness_records(
        "deliveries", "org_acme", where={"task_id": matching_runs[0]["id"]}
    )
    assert len(deliveries) == 1


def test_slack_channels_are_proactive_by_default_with_explicit_opt_out(brain):
    brain.slack_gateway.config.slack_bot_user_id = "UBOT"
    payload = {
        "type": "event_callback",
        "team_id": "TACME",
        "event_id": "EvDefaultProactive",
        "event": {
            "type": "message",
            "user": "UAJAY",
            "channel": "CDEFAULT",
            "channel_type": "channel",
            "text": "The export pipeline is failing again.",
            "ts": "150.001",
        },
    }
    normalized = brain.slack_gateway.normalize(payload)
    assert normalized.payload["proactivity_enabled"] is True

    brain.store.upsert_simple_harness_record(
        "channel_policies",
        {
            "id": "policy_default_opt_out",
            "organization_id": "org_acme",
            "source": "slack",
            "channel_id": "CDEFAULT",
            "enabled": True,
            "observe": True,
            "proactive": False,
            "free_response": False,
            "is_private": False,
        },
    )
    opted_out = brain.slack_gateway.normalize(payload)
    assert opted_out.payload["proactivity_enabled"] is False


def test_slack_has_only_public_private_and_dm_conversation_types(brain):
    brain.slack_gateway.config.slack_bot_user_id = "UBOT"

    def normalized(channel: str, channel_type: str, *, private: bool = False):
        return brain.slack_gateway.normalize(
            {
                "type": "event_callback",
                "team_id": "TACME",
                "event_id": f"Ev-{channel}",
                "event": {
                    "type": "message",
                    "user": "UAJAY",
                    "channel": channel,
                    "channel_type": channel_type,
                    "is_private": private,
                    "text": "hello",
                    "ts": f"1-{channel}",
                    "client_msg_id": f"msg-{channel}",
                },
            }
        )

    public = normalized("CPUBLIC", "channel")
    private = normalized("CPRIVATE", "channel", private=True)
    dm = normalized("DAJAY", "im")

    assert public.scope.visibility == "organization_public"
    assert private.scope.visibility == "private_channel"
    assert dm.scope.visibility == "person_private"
    assert public.payload["context_key"] == "slack:TACME:CPUBLIC"
    assert private.payload["context_key"] == "slack:TACME:CPRIVATE"
    assert dm.payload["context_key"] == "slack:TACME:DAJAY"
    # One Session per room, channels included: the conversation key never
    # forks per message, so two people raising the same issue in a channel
    # reach the same Session instead of two isolated ones.
    assert public.payload["conversation_key"] == "slack:TACME:CPUBLIC"
    assert private.payload["conversation_key"] == "slack:TACME:CPRIVATE"
    assert dm.payload["conversation_key"] == "slack:TACME:DAJAY"
    assert not public.is_direct_agent_request
    assert not private.is_direct_agent_request
    assert dm.is_direct_agent_request


def test_agent_can_change_current_slack_channel_proactivity(brain, monkeypatch):
    # Autonomous mode sends every write through AI review. The reviewer can
    # approve this reversible organization setting without a human pause.
    monkeypatch.setattr(brain.action_reviewer, "review", lambda **_kwargs: "approve")
    destination = {"type": "slack_channel", "id": "CSOCIAL", "is_private": False}
    decision = brain.authz.resolve(
        "org_acme", "person_ajay", destination, "reactive_agent"
    )
    envelope = InvocationEnvelope(
        organization_id="org_acme",
        principal_id="person_ajay",
        conversation_id="slack:TACME:CSOCIAL:175.001",
        destination=destination,
        purpose="reactive_agent",
        permission_decision_id=decision.id,
        allowed_scope_ids=decision.allowed_scope_ids,
    )
    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                tool_calls=[
                    ModelToolCall(
                        id="call_disable_proactivity",
                        name="slack_set_channel_proactivity",
                        arguments={
                            "enabled": False,
                            "reason": "The user asked Sally to stay quiet here.",
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="Proactive attention is now disabled here."),
        ]
    )
    run = brain.company_agent.create_invocation(
        envelope=envelope,
        goal="Stop proactively responding in this channel",
        trigger_kind="reactive",
        initial_message="Sally, stop proactively responding in this channel",
    )
    completed = brain.company_agent.execute_task(run["id"])
    assert completed.state.value == "completed"
    policy = brain.store.channel_policy("org_acme", "slack", "CSOCIAL")
    assert policy["proactive"] is False


def test_ambient_slack_uses_contextual_attention_before_waking(brain):
    brain.slack_gateway.config.slack_bot_user_id = "UBOT"
    brain.store.upsert_simple_harness_record(
        "identity_mappings",
        {
            "id": "identity_slack_ravi",
            "organization_id": "org_acme",
            "source": "slack",
            "source_user_id": "URAVI",
            "principal_id": "person_ravi",
        },
    )
    brain.store.upsert_simple_harness_record(
        "channel_policies",
        {
            "id": "policy_support",
            "organization_id": "org_acme",
            "source": "slack",
            "channel_id": "CSUPPORT",
            "enabled": True,
            "observe": True,
            "proactive": True,
            "free_response": False,
            "is_private": False,
            "scope_id": "scope_org_public",
        },
    )
    payload = {
        "type": "event_callback",
        "team_id": "TACME",
        "event_id": "EvAmbient",
        "event": {
            "type": "message",
            "user": "URAVI",
            "channel": "CSUPPORT",
            "channel_type": "channel",
            "text": "Acme exports are failing again after deployment.",
            "ts": "200.001",
        },
    }
    accepted = brain.slack_gateway.accept(
        body=json.dumps(payload).encode(), headers={}, allow_unsigned=True
    )
    assert accepted["accepted"]
    drained = brain.harness_worker.drain_until_idle()
    assert drained["failed"] == 0
    evaluations = brain.store.list_harness_records(
        "attention_evaluations", "org_acme", where={"event_id": "EvAmbient"}
    )
    assert evaluations
    assert evaluations[0]["decision"] == "WAKE_MAIN_AGENT"
    runs = brain.store.list_tasks("org_acme", worker_type="session_manager")
    proactive = next(
        item for item in runs if item.get("trigger_event_id") == "EvAmbient"
    )
    assert proactive["trigger_kind"] == "proactive"
    assert proactive["state"] == "completed"
    interventions = brain.store.list_harness_records(
        "task_interventions", "org_acme"
    )
    assert any(item["task_id"] == proactive["id"] for item in interventions)


def test_attention_and_main_agent_use_the_same_slack_conversation(brain):
    brain.slack_gateway.config.slack_bot_user_id = "UBOT"
    first_payload = {
        "type": "event_callback",
        "team_id": "TACME",
        "event_id": "EvChannelContextFirst",
        "event": {
            # Slack emits both `message` and `app_mention` for one mentioned
            # message; only the `message` copy carries channel_type, which is
            # what tells Toda the room is public rather than private. Using
            # it here keeps the fixture representative of what actually
            # decides the channel's scope.
            "type": "message",
            "user": "UAJAY",
            "channel": "CSOCIAL",
            "channel_type": "channel",
            "text": "Hey <@UBOT>",
            "ts": "250.001",
            "client_msg_id": "channel-context-first",
        },
    }
    assert brain.slack_gateway.accept(
        body=json.dumps(first_payload).encode(), headers={}, allow_unsigned=True
    )["accepted"]
    assert brain.harness_worker.drain_until_idle()["failed"] == 0
    first_run = next(
        run
        for run in brain.store.list_tasks("org_acme", worker_type="session_manager")
        if run.get("trigger_event_id") == "EvChannelContextFirst"
    )
    assert first_run["final_answer"]

    attention_model = ScriptedModelClient(
        [
            ModelResponse(
                content=json.dumps(
                    {
                        "signal_type": "conversation_followup",
                        "novelty": "continuation",
                        "urgency": "normal",
                        "decision": "WAKE_MAIN_AGENT",
                        "confidence": 0.99,
                        "reason": "The user is responding to Toda's preceding message.",
                        "suggested_goal": "Answer the user's conversational follow-up.",
                        "related_entity_ids": [],
                        "existing_task_id": None,
                    }
                )
            )
        ]
    )
    brain.attention.model = attention_model
    followup_payload = {
        "type": "event_callback",
        "team_id": "TACME",
        "event_id": "EvChannelContextFollowup",
        "event": {
            "type": "message",
            "user": "UAJAY",
            "channel": "CSOCIAL",
            "channel_type": "channel",
            "text": "How can you help me?",
            "ts": "251.001",
            "client_msg_id": "channel-context-followup",
        },
    }
    assert brain.slack_gateway.accept(
        body=json.dumps(followup_payload).encode(), headers={}, allow_unsigned=True
    )["accepted"]
    assert brain.harness_worker.drain_until_idle()["failed"] == 0

    attention_context = json.loads(
        attention_model.requests[0]["messages"][1]["content"]
    )
    timeline = attention_context["recent_conversation"]
    assert len(timeline) <= 10
    assert any(
        item["author"] == "toda" and item["text"] == first_run["final_answer"]
        for item in timeline
    )
    # The current message is supplied separately as new_event, so the literal
    # timeline contains only messages that preceded it.
    assert all(item["text"] != "How can you help me?" for item in timeline)

    followup_run = next(
        run
        for run in brain.store.list_tasks(
            "org_acme", worker_type="session_manager"
        )
        if run.get("trigger_event_id") == "EvChannelContextFollowup"
    )
    main_timeline = brain.company_agent._slack_conversation(followup_run)
    assert any(
        item["role"] == "assistant" and item["text"] == first_run["final_answer"]
        for item in main_timeline
    )


def test_slack_conversation_hides_messages_that_arrived_after_the_task(brain, monkeypatch):
    """Regression test for a real production bug. recent_slack_messages()
    reads raw `received_events` (written the instant Slack delivers), while
    a Task is only created once that message finishes routing into
    `session_messages` -- a lag of over a minute under load. Without a
    cutoff the model is shown a message this Task was never assigned,
    answers it (or worse, acts on it), and then that message's OWN Task
    answers it a second time. Confirmed live: a Task whose goal was only
    "Hey" created a water-reminder automation 60s before that request
    entered the transcript, then the request's own Task replied
    "Already set up" -- one request, two replies."""
    run = {
        "id": "task_lookahead",
        "organization_id": "org_acme",
        "created_at": "2026-08-05T19:25:10+00:00",
        "trigger_event_id": "EvTrigger",
        "metadata": {"slack": {"workspace_id": "TACME", "channel_id": "CSOCIAL"}},
        "envelope": {"destination": {"id": "CSOCIAL"}},
    }
    monkeypatch.setattr(
        brain.company_agent.runs.store,
        "recent_slack_messages",
        lambda *_args, **_kwargs: [
            {"at": "2026-08-05T19:24:00+00:00", "message_id": "EvOld", "text": "earlier context", "role": "user"},
            {"at": "2026-08-05T19:25:13+00:00", "message_id": "EvFuture", "text": "can u ping me every 3 mins", "role": "user"},
        ],
    )

    timeline = brain.company_agent._slack_conversation(run)

    assert [item["message_id"] for item in timeline] == ["EvOld"]


def test_wake_preserves_slack_conversation_address_for_main_context(brain):
    from toda.harness.models import (
        AttentionDecision,
        AttentionEvaluation,
        EventActor,
        EventScope,
        NormalizedEvent,
    )

    context_key = "slack:TACME:CSUPPORT2"
    conversation_key = f"{context_key}:event:EvContext2"
    event = NormalizedEvent(
        id="EvContext2",
        organization_id="org_acme",
        source="slack",
        type="slack.channel_message",
        actor=EventActor(id="person_ravi", source_id="URAVI2", type="person"),
        scope=EventScope(
            workspace_id="TACME",
            channel_id="CSUPPORT2",
            visibility="organization_public",
            scope_id="scope_org_public",
        ),
        payload={
            "text": "Widget exports are failing again after deployment.",
            "conversation_key": conversation_key,
            "context_key": context_key,
            "proactivity_enabled": True,
        },
        dedupe_key="dedupe-EvContext2",
        is_direct_agent_request=False,
    )
    evaluation = AttentionEvaluation(
        signal_type="possible_operational_issue",
        novelty="new",
        urgency="high",
        decision=AttentionDecision.WAKE_MAIN_AGENT,
        confidence=0.9,
        reason="Deterministic local evaluator found an actionable failure signal.",
    )

    wake = brain.attention._wake_session(event, evaluation)
    run = brain.session_manager.dispatch(wake["session_id"])
    assert run is not None
    run = brain.store.get_task(run.task_id)
    assert run["metadata"]["slack"] == {
        "workspace_id": "TACME",
        "channel_id": "CSUPPORT2",
        "context_key": context_key,
        "actor_profile": {
            "principal_id": "person_ravi",
            "source_user_id": "URAVI2",
            "display_name": "person_ravi",
        },
    }


def test_stale_active_session_does_not_strand_ambient_messages(brain):
    """A conversation's active_session_id is only ever set by attention's
    own wake path and is never cleared on completion -- if that Session's
    root Task already finished (e.g. a direct-mention Task answered and
    completed, bypassing attention entirely) and a later ambient message
    references the same conversation, the old code would silently attach
    the new message to the dead pointer and never process it. It must
    instead treat the stale pointer as no active work and wake a fresh
    root Task (find_root_task_for_session excludes terminal states,
    exactly like the old stale-root check did)."""
    from toda.domain.models import utcnow
    from toda.harness.model_client import ScriptedModelClient
    from toda.harness.models import EventActor, EventScope, ModelResponse, NormalizedEvent

    context_key = "slack:TACME:CSTALE"
    brain.company_agent.model = ScriptedModelClient(
        [ModelResponse(content="Earlier finished task done.")]
    )
    wake = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key=context_key,
        destination={"type": "slack_channel", "id": "CSTALE", "is_private": False},
        role="user",
        content="Earlier finished task",
        principal_id="person_ajay",
    )
    brain.session_manager.dispatch(wake["session_id"])
    stale_root = brain.store.list_tasks(
        "org_acme", session_id=wake["session_id"], worker_type="session_manager"
    )[0]
    assert stale_root["state"] == "completed"

    brain.store.upsert_conversation_state(
        {
            "id": "conversation_state_stale_test",
            "organization_id": "org_acme",
            "conversation_key": context_key,
            "workspace_id": "TACME",
            "channel_id": "CSTALE",
            "participants": [],
            "recent_messages": [],
            "active_session_id": wake["session_id"],
            "last_agent_intervention_at": None,
            "last_human_acknowledgement_at": None,
            "updated_at": utcnow().isoformat(),
        }
    )

    brain.attention.model = ScriptedModelClient(
        [
            {
                "content": json.dumps(
                    {
                        "signal_type": "possible_operational_issue",
                        "novelty": "new",
                        "urgency": "high",
                        "decision": "WAKE_MAIN_AGENT",
                        "confidence": 0.95,
                        "reason": "New actionable issue.",
                        "suggested_goal": "Investigate the new issue.",
                        "related_entity_ids": [],
                        "existing_task_id": None,
                    }
                )
            }
        ]
    )
    event = NormalizedEvent(
        id="EvStaleFollowup",
        organization_id="org_acme",
        source="slack",
        type="slack.channel_message",
        actor=EventActor(id="person_ajay", source_id="UAJAY", type="person"),
        scope=EventScope(
            workspace_id="TACME",
            channel_id="CSTALE",
            visibility="organization_public",
            scope_id="scope_org_public",
        ),
        payload={
            "text": "try again!",
            "conversation_key": f"{context_key}:event:EvStaleFollowup",
            "context_key": context_key,
            "proactivity_enabled": True,
        },
        dedupe_key="dedupe-EvStaleFollowup",
        is_direct_agent_request=False,
    )

    result = brain.attention.process(event)
    assert result["evaluation"]["decision"] == "WAKE_MAIN_AGENT"
    assert "session_id" in result
    assert "job_id" in result
    # Same Session (stable per conversation_key) but the dispatch tick must
    # start a fresh root Task, not silently attach to the dead one.
    brain.harness_worker.drain_until_idle()
    fresh_tasks = [
        t
        for t in brain.store.list_tasks(
            "org_acme", session_id=wake["session_id"], worker_type="session_manager"
        )
        if t["id"] != stale_root["id"]
    ]
    assert len(fresh_tasks) == 1

    updated_conversation = brain.store.conversation_state("org_acme", context_key)
    assert updated_conversation["active_session_id"] == result["session_id"]


def _create_onboarding_role(brain, **overrides) -> dict:
    role = {
        "id": "role_onboarding",
        "organization_id": "org_acme",
        "name": "New hire onboarding",
        "goal": "Kick off the onboarding skill for the incoming new hire.",
        "skill_ids": ["skill_onboarding"],
        "trigger_description": "a new hire starting soon",
        "status": "active",
    }
    role.update(overrides)
    brain.store.upsert_simple_harness_record("roles", role)
    return role


def test_role_trigger_wakes_the_main_agent_with_matching_goal(brain):
    from toda.harness.models import EventActor, EventScope, NormalizedEvent

    role = _create_onboarding_role(brain)
    context_key = "slack:TACME:CHIRING"
    conversation_key = f"{context_key}:event:EvNewHire"
    event = NormalizedEvent(
        id="EvNewHire",
        organization_id="org_acme",
        source="slack",
        type="slack.channel_message",
        actor=EventActor(id="person_ravi", source_id="URAVI3", type="person"),
        scope=EventScope(
            workspace_id="TACME",
            channel_id="CHIRING",
            visibility="organization_public",
            scope_id="scope_org_public",
        ),
        payload={
            "text": "We have a new hire starting soon, please help get her set up.",
            "conversation_key": conversation_key,
            "context_key": context_key,
            "proactivity_enabled": True,
        },
        dedupe_key="dedupe-EvNewHire",
        is_direct_agent_request=False,
    )

    result = brain.attention.process(event)
    assert result["evaluation"]["decision"] == "WAKE_MAIN_AGENT"
    assert result["evaluation"]["matched_role_id"] == role["id"]
    assert result["evaluation"]["suggested_goal"] == role["goal"]
    assert "session_id" in result
    brain.harness_worker.drain_until_idle()
    run = brain.store.find_root_task_for_session(result["session_id"]) or next(
        t
        for t in brain.store.list_tasks(
            "org_acme", session_id=result["session_id"], worker_type="session_manager"
        )
    )
    assert run["metadata"]["role_skill_ids"] == ["skill_onboarding"]


def test_role_with_no_matching_event_does_not_spuriously_wake(brain):
    from toda.harness.models import EventActor, EventScope, NormalizedEvent

    _create_onboarding_role(brain)
    context_key = "slack:TACME:CFINANCE"
    conversation_key = f"{context_key}:event:EvUnrelated"
    event = NormalizedEvent(
        id="EvUnrelated",
        organization_id="org_acme",
        source="slack",
        type="slack.channel_message",
        actor=EventActor(id="person_ravi", source_id="URAVI4", type="person"),
        scope=EventScope(
            workspace_id="TACME",
            channel_id="CFINANCE",
            visibility="organization_public",
            scope_id="scope_org_public",
        ),
        payload={
            "text": "Can someone double check the quarterly report numbers?",
            "conversation_key": conversation_key,
            "context_key": context_key,
            "proactivity_enabled": True,
        },
        dedupe_key="dedupe-EvUnrelated",
        is_direct_agent_request=False,
    )

    result = brain.attention.process(event)
    assert result["evaluation"]["matched_role_id"] is None
    assert not any(
        run.get("trigger_event_id") == "EvUnrelated"
        for run in brain.store.list_tasks(
            "org_acme", worker_type="session_manager"
        )
    )


def test_role_api_create_and_list(api_client):
    created = api_client.post(
        "/v1/roles",
        json={
            "organization_id": "org_acme",
            "name": "New hire onboarding",
            "trigger_description": "a new hire starting soon",
            "goal": "Kick off the onboarding skill for the incoming new hire.",
            "skill_ids": ["skill_onboarding"],
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "active"
    assert body["id"]

    listed = api_client.get("/v1/roles", params={"organization_id": "org_acme"})
    assert listed.status_code == 200
    assert listed.json()["count"] >= 1
    assert any(r["id"] == body["id"] for r in listed.json()["roles"])


def test_ambient_channel_without_policy_is_proactive_by_default(brain):
    brain.slack_gateway.config.slack_bot_user_id = "UBOT"
    payload = {
        "type": "event_callback",
        "team_id": "TACME",
        "event_id": "EvNoWake",
        "event": {
            "type": "message",
            "user": "UNEW",
            "channel": "CRANDOM",
            "channel_type": "channel",
            "text": "The service is failing and the customer is blocked.",
            "ts": "300.001",
        },
    }
    brain.slack_gateway.accept(
        body=json.dumps(payload).encode(), headers={}, allow_unsigned=True
    )
    brain.harness_worker.drain_until_idle()
    evaluations = brain.store.list_harness_records(
        "attention_evaluations", "org_acme", where={"event_id": "EvNoWake"}
    )
    assert evaluations[0]["decision"] == "WAKE_MAIN_AGENT"
    assert any(
        run.get("trigger_event_id") == "EvNoWake"
        for run in brain.store.list_tasks(
            "org_acme", worker_type="session_manager"
        )
    )


def test_api_invoke_exposes_complete_trace(api_client):
    response = api_client.post(
        "/v1/tasks/invoke",
        json={
            "organization_id": "org_acme",
            "principal_id": "person_ajay",
            "conversation_id": "api:e2e:trace",
            "destination": {
                "type": "internal_agent_session",
                "id": "api-e2e",
                "is_private": True,
            },
            "prompt": "What happened with Acme exports?",
            "run_now": True,
        },
    )
    assert response.status_code == 200, response.text
    run = next(task for task in response.json()["tasks"] if task["worker_type"] == "session_manager")
    assert run["state"] == "completed"
    trace = api_client.get(f"/v1/tasks/{run['id']}", params={"organization_id": "org_acme"}).json()
    assert trace["messages"]
    assert trace["turns"]
    assert trace["checkpoints"]
    assert trace["task"]["trace_id"]
    evaluation = api_client.get(
        "/v1/harness/evaluation", params={"organization_id": "org_acme"}
    )
    assert evaluation.status_code == 200
    assert evaluation.json()["tasks"]["count"] >= 1


def test_agent_can_wait_for_human_and_resume(brain):
    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                tool_calls=[
                    ModelToolCall(
                        id="call_human",
                        name="request_human_input",
                        arguments={
                            "question": "Which Linear team should own this?",
                            "reason": "The request did not identify a team.",
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="Thanks. I will use the Platform team."),
        ]
    )
    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="api:human:thread"),
        goal="Prepare the issue",
        trigger_kind="reactive",
        initial_message="Prepare the issue",
    )
    outcome = brain.company_agent.execute_task(run["id"])
    assert outcome.state.value == "waiting_for_human"
    assert "Which Linear team" in outcome.answer
    brain.harness_worker.drain_until_idle()
    resumed = brain.company_agent.resume_with_human_message(
        run["id"],
        message="Platform",
        principal_id="person_ajay",
    )
    assert resumed.state.value == "completed"
    assert "Platform" in resumed.answer


def test_automation_activates_same_agent_runtime(brain):
    automation = brain.automations.create(
        organization_id="org_acme",
        name="Export health digest",
        goal="Summarize current export issues",
        principal_id="person_ajay",
        conversation_id="automation:export-digest",
        destination={
            "type": "internal_agent_session",
            "id": "digest",
            "is_private": True,
        },
        interval_seconds=60,
    )
    automation["next_run_at"] = "2000-01-01T00:00:00+00:00"
    brain.automations._upsert(automation)
    tick = brain.automations.tick("org_acme")
    assert tick["fired"] == 1
    brain.harness_worker.drain_until_idle()
    run = brain.store.list_tasks(
        "org_acme",
        session_id=tick["session_ids"][0],
        worker_type="session_manager",
    )[0]
    assert run["trigger_kind"] == "automation"
    assert run["state"] == "completed"
    assert run["envelope"]["purpose"] == "scheduled_automation"


def test_feedback_updates_attention_precision_metrics(brain):
    item = brain.harness_evaluation.record_feedback(
        organization_id="org_acme",
        feedback_type="helpful",
        principal_id="person_ajay",
        task_id=None,
        comment="Useful intervention",
    )
    assert item["id"]
    metrics = brain.harness_evaluation.metrics("org_acme")
    assert metrics["feedback"]["helpful"] == 1


def test_invalid_slack_signature_is_rejected(brain):
    brain.slack_gateway.config.slack_signing_secret = "correct-secret"
    payload = {
        "type": "event_callback",
        "team_id": "TACME",
        "event_id": "EvBadSignature",
        "event": {
            "type": "message",
            "user": "UAJAY",
            "channel": "D123",
            "channel_type": "im",
            "text": "hello",
            "ts": "400.001",
        },
    }
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    try:
        brain.slack_gateway.accept(
            body=body,
            headers={
                "x-slack-request-timestamp": timestamp,
                "x-slack-signature": "v0=wrong",
            },
        )
    except PermissionError:
        pass
    else:
        raise AssertionError("invalid signature must fail closed")


def test_operational_dashboard_and_list_routes(api_client):
    invoked = api_client.post(
        "/v1/tasks/invoke",
        json={
            "organization_id": "org_acme",
            "principal_id": "person_ajay",
            "conversation_id": "api:dashboard",
            "destination": {
                "type": "internal_agent_session",
                "id": "dashboard",
                "is_private": True,
            },
            "prompt": "Summarize the export incident",
            "run_now": True,
        },
    )
    assert invoked.status_code == 200
    listed = api_client.get(
        "/v1/tasks", params={"organization_id": "org_acme"}
    )
    assert listed.status_code == 200
    assert listed.json()["count"] >= 1
    dashboard = api_client.get(
        "/v1/harness/dashboard",
        params={"organization_id": "org_acme"},
    )
    assert dashboard.status_code == 200, dashboard.text
    payload = dashboard.json()
    assert payload["evaluation"]["tasks"]["count"] >= 1
    assert "counts" in payload["jobs"]
    status = api_client.get("/v1/harness/status")
    assert status.status_code == 200
    assert status.json()["database"]["backend"] == "sqlite"
    assert "connections_configured" in status.json()["models"]
    assert api_client.get("/admin").status_code == 200


def test_retention_is_previewable_and_requires_explicit_apply(api_client):
    preview = api_client.post(
        "/v1/admin/retention",
        json={
            "organization_id": "org_acme",
            "older_than_days": 3650,
            "apply": False,
        },
    )
    assert preview.status_code == 200
    assert "counts" in preview.json()
    rejected = api_client.post(
        "/v1/admin/retention",
        json={
            "organization_id": "org_acme",
            "older_than_days": 3650,
            "apply": True,
        },
    )
    assert rejected.status_code == 400


def test_tenant_deletion_requires_exact_confirmation(api_client):
    rejected = api_client.post(
        "/v1/admin/organizations:delete",
        json={
            "organization_id": "org_acme",
            "confirmation": "wrong-org",
        },
    )
    assert rejected.status_code == 400
    deleted = api_client.post(
        "/v1/admin/organizations:delete",
        json={
            "organization_id": "org_acme",
            "confirmation": "org_acme",
        },
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted"]["organizations"] == 1
    metrics = api_client.get(
        "/v1/metrics", params={"organization_id": "org_acme"}
    )
    assert metrics.json()["sources"] == 0


def test_deferred_profile_refresh_and_reindex_jobs(api_client):
    profile_job = api_client.post(
        "/v1/admin/profiles:refresh",
        json={
            "organization_id": "org_acme",
            "entity_id": "project_toda",
        },
    )
    reindex_job = api_client.post(
        "/v1/admin/reindex",
        json={"organization_id": "org_acme"},
    )
    assert profile_job.status_code == 200
    assert reindex_job.status_code == 200
    drained = api_client.post("/v1/harness/worker:drain")
    assert drained.status_code == 200
    assert drained.json()["failed"] == 0


def test_transient_model_failure_is_retried_by_durable_queue(brain):
    class FlakyModel:
        model = "flaky"

        def __init__(self):
            self.calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("503 service unavailable")
            return ModelResponse(content="Recovered after provider retry.")

    brain.company_agent.model = FlakyModel()
    wake = brain.sessions.wake(
        organization_id="org_acme",
        conversation_key="api:retry",
        destination={"type": "internal_agent_session", "id": "test", "is_private": True},
        role="user",
        content="Retry a transient model failure",
        principal_id="person_ajay",
    )
    job = brain.harness_jobs.enqueue(
        organization_id="org_acme",
        kind="session.dispatch",
        payload={"session_id": wake["session_id"]},
        idempotency_key=f"session.dispatch:{wake['session_id']}",
    )
    first = brain.harness_worker.drain_once()
    assert first["failed"] == 1
    queued = brain.store.get_harness_job(job["id"])
    assert queued["status"] == "queued"
    queued["available_at"] = "2000-01-01T00:00:00+00:00"
    with brain.store.cursor() as cur:
        cur.execute(
            """UPDATE harness_jobs SET available_at = ?, payload = ?
            WHERE id = ?""",
            (
                queued["available_at"],
                json.dumps(queued),
                queued["id"],
            ),
        )
    second = brain.harness_worker.drain_until_idle()
    assert second["failed"] == 0
    tasks = brain.store.list_tasks(
        "org_acme", session_id=wake["session_id"], worker_type="session_manager"
    )
    assert any(task["state"] == "completed" for task in tasks)
