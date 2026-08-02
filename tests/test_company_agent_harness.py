"""End-to-end tests for the managed Hermes-style company agent harness."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from docket.harness.model_client import ScriptedModelClient
from docket.harness.models import (
    InvocationEnvelope,
    ModelResponse,
    ModelToolCall,
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
    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain),
        goal="What do we know about the Acme export failure?",
        trigger_kind="reactive",
        initial_message="What do we know about the Acme export failure?",
    )
    brain.harness_jobs.enqueue(
        organization_id="org_acme",
        kind="agent.run",
        payload={"run_id": run["id"]},
        idempotency_key=f"agent.run:{run['id']}",
    )
    drained = brain.harness_worker.drain_until_idle()
    assert drained["failed"] == 0

    stored = brain.agent_runs.get(run["id"])
    assert stored["state"] == "completed"
    assert "permitted company context" in stored["final_answer"]
    assert stored["iteration_count"] == 1
    assert brain.store.list_harness_records(
        "agent_turns", "org_acme", where={"run_id": run["id"]}
    )
    checkpoints = brain.store.list_harness_records(
        "checkpoints", "org_acme", where={"run_id": run["id"]}
    )
    assert {item["state"] for item in checkpoints} >= {
        "created",
        "queued",
        "running",
        "completed",
    }
    captured = [
        source
        for source in brain.store.list_sources("org_acme")
        if source.get("external_id") == run["id"]
        and source.get("source_type") == "agent_run"
    ]
    assert captured, "completed run must be captured asynchronously"


def test_write_tool_pauses_and_resumes_from_approval(brain):
    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ModelToolCall(
                        id="call_create_linear",
                        name="linear_create_issue",
                        arguments={
                            "arguments": {
                                "title": "Acme export failure",
                                "description": "Recurring customer failure",
                            }
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ModelResponse(
                content=(
                    "The approved action could not execute because the external "
                    "connection is not configured; no issue was claimed as created."
                )
            ),
        ]
    )
    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="api:approval:thread"),
        goal="Create a Linear issue for Acme exports",
        trigger_kind="reactive",
        initial_message="Create a Linear issue for Acme exports",
    )
    outcome = brain.company_agent.run(run["id"])
    assert outcome.state.value == "waiting_for_approval"
    approval = brain.store.get_harness_record("approvals", outcome.approval_id)
    assert approval["status"] == "pending"
    assert approval["tool_name"] == "linear_create_issue"

    resumed = brain.company_agent.resume_after_approval(
        approval_id=approval["id"],
        approved=True,
        decided_by="person_ajay",
    )
    assert resumed.state.value == "completed"
    decided = brain.store.get_harness_record("approvals", approval["id"])
    assert decided["status"] == "approved"
    calls = brain.store.list_harness_records(
        "harness_tool_calls", "org_acme", where={"run_id": run["id"]}
    )
    assert calls[0]["status"] == "failed"
    assert "not configured" in calls[0]["error"]


def test_parallel_read_tool_calls_execute_concurrently(brain):
    """H-08: independent read-risk tool calls proposed in one iteration must
    run concurrently, not one after another."""
    from docket.harness.models import ToolDefinition

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
    outcome = brain.company_agent.run(run["id"])
    elapsed = time.monotonic() - started

    assert outcome.state.value == "completed"
    # Serial execution would take >=0.6s; concurrent execution should finish
    # in roughly one call's duration plus overhead.
    assert elapsed < 0.55, f"expected concurrent execution, took {elapsed:.2f}s"
    labels_started = {name.split(":")[1] for name, _ in call_log if name.startswith("start")}
    assert labels_started == {"a", "b"}

    calls = brain.store.list_harness_records(
        "harness_tool_calls", "org_acme", where={"run_id": run["id"]}
    )
    assert len(calls) == 2
    assert all(call["status"] == "completed" for call in calls)


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
                        name="linear_create_issue",
                        arguments={
                            "arguments": {
                                "title": "Acme export failure",
                                "description": "Recurring customer failure",
                            }
                        },
                    ),
                ],
                finish_reason="tool_calls",
            ),
        ]
    )
    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="api:mixed-risk:thread"),
        goal="Look up context then file a ticket",
        trigger_kind="reactive",
        initial_message="Look up context then file a ticket",
    )
    outcome = brain.company_agent.run(run["id"])
    assert outcome.state.value == "waiting_for_approval"

    messages = brain.agent_runs.messages(run["session_id"])
    tool_messages = [m for m in messages if m["role"] == "tool"]
    assert any(m.get("tool_call_id") == "call_read" for m in tool_messages)
    assert not any(m.get("tool_call_id") == "call_write" for m in tool_messages)


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


def test_running_agent_receives_a_steer_signal_instead_of_a_duplicate_run(brain):
    """A message arriving while a run is genuinely RUNNING must fold into
    that run, not spawn a second, independent one in the same conversation."""
    conversation_key = "slack:TACME:Csteer:1"
    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation=conversation_key),
        goal="Investigate the export failure",
        trigger_kind="reactive",
        initial_message="Investigate the export failure",
    )
    brain.agent_runs.transition(run["id"], "running", force=True)

    result = brain.event_router.route(
        _reactive_event_payload(
            event_id="ev_steer_1",
            conversation_key=conversation_key,
            text="Actually also check the billing service",
        )
    )
    assert result["route"] == "run_signal"
    assert result["signal"] == "steer"
    assert result["run_id"] == run["id"]

    refreshed = brain.agent_runs.get(run["id"])
    assert refreshed["pending_signal"] == {
        "kind": "steer",
        "message": "Actually also check the billing service",
    }
    matching = [
        r
        for r in brain.store.list_harness_records("agent_runs", "org_acme")
        if r.get("goal") == "Investigate the export failure"
    ]
    assert len(matching) == 1


def test_running_agent_receives_an_abort_signal_on_stop_like_message(brain):
    conversation_key = "slack:TACME:Cabort:1"
    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation=conversation_key),
        goal="Investigate the export failure",
        trigger_kind="reactive",
        initial_message="Investigate the export failure",
    )
    brain.agent_runs.transition(run["id"], "running", force=True)

    result = brain.event_router.route(
        _reactive_event_payload(
            event_id="ev_abort_1",
            conversation_key=conversation_key,
            text="stop",
            channel_id="Cabort",
        )
    )
    assert result["route"] == "run_signal"
    assert result["signal"] == "abort"
    refreshed = brain.agent_runs.get(run["id"])
    assert refreshed["pending_signal"] == {"kind": "abort", "message": "stop"}


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
    outcome = brain.company_agent.run(run["id"])
    assert outcome.state.value == "completed"

    messages = brain.agent_runs.messages(run["session_id"])
    steered = [m for m in messages if (m.get("metadata") or {}).get("steered")]
    assert steered and steered[0]["content"] == "Focus on Q3 only"
    # The steer message must have reached the model that produced the answer.
    request = model.requests[0]
    assert any(m.get("content") == "Focus on Q3 only" for m in request["messages"])

    refreshed = brain.agent_runs.get(run["id"])
    assert not refreshed.get("pending_signal")


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
    outcome = brain.company_agent.run(run["id"])
    assert outcome.state.value == "cancelled"
    assert model.requests == []


def _pending_approval(brain, *, conversation: str) -> str:
    brain.company_agent.model = ScriptedModelClient(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ModelToolCall(
                        id="call_create_linear",
                        name="linear_create_issue",
                        arguments={
                            "arguments": {
                                "title": "Acme export failure",
                                "description": "Recurring customer failure",
                            }
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
        goal="Create a Linear issue for Acme exports",
        trigger_kind="reactive",
        initial_message="Create a Linear issue for Acme exports",
    )
    outcome = brain.company_agent.run(run["id"])
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
            action_id="docket_approve",
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
            action_id="docket_approve",
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
    from docket.harness.slack import UnauthorizedApprover

    approval_id = _pending_approval(brain, conversation="api:socket-approval:unauth")
    # No identity mapping is registered for this Slack user, so it resolves
    # to "slack:U_STRANGER" — not the requester ("person_ajay") and not in
    # allowed_approver_ids.
    try:
        brain.slack_gateway.accept_interaction(
            body=_interaction_payload(
                action_id="docket_approve",
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
    outcome = brain.company_agent.run(run["id"])
    assert outcome.state.value == "waiting_for_human"
    return run["id"], choices


def _choice_interaction_payload(
    *, action_id: str, run_id: str, index: int, user_id: str, is_select: bool = False
) -> bytes:
    value = f"{run_id}::{index}"
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
    run_id, choices = _pending_question_with_choices(brain, conversation="api:choice-button:thread")
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
            action_id="docket_choice", run_id=run_id, index=0, user_id="U_CHOOSER"
        ),
        allow_unsigned=True,
    )
    assert result["accepted"] is True

    drained = brain.harness_worker.drain_until_idle()
    assert drained["failed"] == 0
    stored = brain.agent_runs.get(run_id)
    assert stored["state"] == "completed"
    messages = brain.agent_runs.messages(stored["session_id"], limit=50)
    assert any(m["content"] == choices[0] and m["role"] == "user" for m in messages)


def test_choice_select_click_resumes_run_with_selected_text(brain):
    """The same run_id::index encoding resolves identically whether Slack
    rendered buttons or a static_select — the resolver doesn't care which
    UI produced the click."""
    run_id, choices = _pending_question_with_choices(brain, conversation="api:choice-select:thread")
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
            action_id="docket_choice_select",
            run_id=run_id,
            index=1,
            user_id="U_CHOOSER2",
            is_select=True,
        ),
        allow_unsigned=True,
    )
    assert result["accepted"] is True
    drained = brain.harness_worker.drain_until_idle()
    assert drained["failed"] == 0
    messages = brain.agent_runs.messages(brain.agent_runs.get(run_id)["session_id"], limit=50)
    assert any(m["content"] == choices[1] and m["role"] == "user" for m in messages)


def test_choice_interaction_on_already_resumed_run_is_a_noop(brain):
    run_id, choices = _pending_question_with_choices(brain, conversation="api:choice-stale:thread")
    brain.company_agent.model = ScriptedModelClient([ModelResponse(content="Done.")])
    first = brain.slack_gateway.accept_interaction(
        body=_choice_interaction_payload(
            action_id="docket_choice", run_id=run_id, index=0, user_id="U_FIRST"
        ),
        allow_unsigned=True,
    )
    assert first["accepted"] is True
    brain.harness_worker.drain_until_idle()
    assert brain.agent_runs.get(run_id)["state"] == "completed"

    # A second, stale click (different choice, e.g. a slow double-tap) must
    # not raise or attempt to resume a run that has already finished.
    second = brain.slack_gateway.accept_interaction(
        body=_choice_interaction_payload(
            action_id="docket_choice", run_id=run_id, index=1, user_id="U_SECOND"
        ),
        allow_unsigned=True,
    )
    assert second["accepted"] is False


def test_deliver_question_renders_buttons_or_select_by_choice_count(brain):
    run_id, choices = _pending_question_with_choices(brain, conversation="api:choice-render:thread")

    few = brain.delivery.deliver_question(run_id, "Pick one", choices=choices)
    few_blocks = few["delivery"]["provider_result"]["blocks"]
    assert any(b["type"] == "actions" for b in few_blocks)

    many = [f"Option {i}" for i in range(8)]
    result = brain.delivery.deliver_question(run_id, "Pick one of many", choices=many)
    many_blocks = result["delivery"]["provider_result"]["blocks"]
    assert many_blocks[0]["accessory"]["type"] == "static_select"
    assert len(many_blocks[0]["accessory"]["options"]) == 8

    plain = brain.delivery.deliver_question(run_id, "No choices here")
    assert plain["delivery"]["provider_result"]["blocks"] == []


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
    runs = brain.store.list_harness_records("agent_runs", "org_acme")
    run = next(item for item in runs if item["trigger_event_id"] == "Ev100")
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
        for run in brain.store.list_harness_records("agent_runs", "org_acme")
        if run.get("trigger_event_id")
        in {"EvMessageVariant", "EvMentionVariant"}
    ]
    assert len(matching_runs) == 1
    deliveries = brain.store.list_harness_records(
        "deliveries", "org_acme", where={"run_id": matching_runs[0]["id"]}
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
    assert public.payload["conversation_key"].startswith(
        "slack:TACME:CPUBLIC:event:"
    )
    assert private.payload["conversation_key"].startswith(
        "slack:TACME:CPRIVATE:event:"
    )
    assert dm.payload["conversation_key"] == "slack:TACME:DAJAY"
    assert not public.is_direct_agent_request
    assert not private.is_direct_agent_request
    assert dm.is_direct_agent_request


def test_agent_can_change_current_slack_channel_proactivity_after_approval(brain):
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
    waiting = brain.company_agent.run(run["id"])
    assert waiting.state.value == "waiting_for_approval"
    completed = brain.company_agent.resume_after_approval(
        approval_id=waiting.approval_id,
        approved=True,
        decided_by="person_ajay",
    )
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
    runs = brain.store.list_harness_records("agent_runs", "org_acme")
    proactive = next(
        item for item in runs if item.get("trigger_event_id") == "EvAmbient"
    )
    assert proactive["trigger_kind"] == "proactive"
    assert proactive["state"] == "completed"
    interventions = brain.store.list_harness_records(
        "agent_interventions", "org_acme"
    )
    assert any(item["run_id"] == proactive["id"] for item in interventions)


def test_attention_and_main_agent_use_the_same_slack_conversation(brain):
    brain.slack_gateway.config.slack_bot_user_id = "UBOT"
    first_payload = {
        "type": "event_callback",
        "team_id": "TACME",
        "event_id": "EvChannelContextFirst",
        "event": {
            "type": "app_mention",
            "user": "UAJAY",
            "channel": "CSOCIAL",
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
        for run in brain.store.list_harness_records("agent_runs", "org_acme")
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
                        "reason": "The user is responding to Docket's preceding message.",
                        "suggested_goal": "Answer the user's conversational follow-up.",
                        "related_entity_ids": [],
                        "existing_run_id": None,
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
        item["author"] == "docket" and item["text"] == first_run["final_answer"]
        for item in timeline
    )
    # The current message is supplied separately as new_event, so the literal
    # timeline contains only messages that preceded it.
    assert all(item["text"] != "How can you help me?" for item in timeline)

    followup_run = next(
        run
        for run in brain.store.list_harness_records("agent_runs", "org_acme")
        if run.get("trigger_event_id") == "EvChannelContextFollowup"
    )
    main_timeline = brain.company_agent._slack_conversation(followup_run)
    assert any(
        item["role"] == "assistant" and item["text"] == first_run["final_answer"]
        for item in main_timeline
    )


def test_wake_preserves_slack_conversation_address_for_main_context(brain):
    from docket.harness.models import (
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

    run = brain.attention._wake(event, evaluation)
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
        for run in brain.store.list_harness_records("agent_runs", "org_acme")
    )


def test_api_invoke_exposes_complete_trace(api_client):
    response = api_client.post(
        "/v1/agent/invoke",
        json={
            "organization_id": "org_acme",
            "principal_id": "person_ajay",
            "conversation_id": "api:e2e:trace",
            "destination": {
                "type": "internal_agent_session",
                "id": "api-e2e",
                "is_private": True,
            },
            "goal": "What happened with Acme exports?",
            "run_now": True,
        },
    )
    assert response.status_code == 200, response.text
    run = response.json()["run"]
    assert run["state"] == "completed"
    trace = api_client.get(f"/v1/agent/runs/{run['id']}").json()
    assert trace["messages"]
    assert trace["turns"]
    assert trace["checkpoints"]
    assert trace["run"]["trace_id"]
    evaluation = api_client.get(
        "/v1/harness/evaluation", params={"organization_id": "org_acme"}
    )
    assert evaluation.status_code == 200
    assert evaluation.json()["runs"]["count"] >= 1


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
    outcome = brain.company_agent.run(run["id"])
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
    run = brain.agent_runs.get(tick["run_ids"][0])
    assert run["trigger_kind"] == "automation"
    assert run["state"] == "completed"


def test_feedback_updates_attention_precision_metrics(brain):
    item = brain.harness_evaluation.record_feedback(
        organization_id="org_acme",
        feedback_type="helpful",
        principal_id="person_ajay",
        run_id=None,
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
        "/v1/agent/invoke",
        json={
            "organization_id": "org_acme",
            "principal_id": "person_ajay",
            "conversation_id": "api:dashboard",
            "destination": {
                "type": "internal_agent_session",
                "id": "dashboard",
                "is_private": True,
            },
            "goal": "Summarize the export incident",
            "run_now": True,
        },
    )
    assert invoked.status_code == 200
    listed = api_client.get(
        "/v1/agent/runs", params={"organization_id": "org_acme"}
    )
    assert listed.status_code == 200
    assert listed.json()["count"] >= 1
    dashboard = api_client.get(
        "/v1/harness/dashboard",
        params={"organization_id": "org_acme"},
    )
    assert dashboard.status_code == 200, dashboard.text
    payload = dashboard.json()
    assert payload["evaluation"]["runs"]["count"] >= 1
    assert "counts" in payload["jobs"]
    status = api_client.get("/v1/harness/status")
    assert status.status_code == 200
    assert status.json()["database"]["backend"] == "sqlite"
    assert "model_key_configured" in status.json()["models"]
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
            "entity_id": "project_docket",
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
    run = brain.company_agent.create_invocation(
        envelope=_envelope(brain, conversation="api:retry"),
        goal="Retry a transient model failure",
        trigger_kind="reactive",
        initial_message="Retry a transient model failure",
    )
    job = brain.harness_jobs.enqueue(
        organization_id="org_acme",
        kind="agent.run",
        payload={"run_id": run["id"]},
        idempotency_key=f"agent.run:{run['id']}",
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
    assert brain.agent_runs.get(run["id"])["state"] == "completed"
