"""End-to-end tests for the managed Hermes-style company agent harness."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from company_brain.harness.model_client import ScriptedModelClient
from company_brain.harness.models import (
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


def test_ambient_channel_without_opt_in_does_not_wake(brain):
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
    assert evaluations[0]["decision"] == "ADD_TO_DIGEST"
    assert not any(
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
    assert "openrouter_key_configured" in status.json()["models"]
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
            "entity_id": "project_company_brain",
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
