"""slack_send_artifact only ever shares content the acting principal and
destination are actually authorized to see, and behaves safely with no
Slack bot token configured (the default in tests — see conftest.py).

These tests exercise delivery authorization after the policy decision has
already been resolved; mode/reviewer behavior is covered separately.
"""

from __future__ import annotations

from docket.harness.models import InvocationEnvelope, ModelToolCall


def _envelope(brain, *, destination: dict) -> InvocationEnvelope:
    decision = brain.authz.resolve("org_acme", "person_ajay", destination, "reactive_agent")
    return InvocationEnvelope(
        organization_id="org_acme",
        principal_id="person_ajay",
        conversation_id=f"api:artifact-test:{destination['id']}",
        destination=destination,
        purpose="reactive_agent",
        permission_decision_id=decision.id,
        allowed_scope_ids=decision.allowed_scope_ids,
    )


def _run(brain, destination: dict):
    envelope = _envelope(brain, destination=destination)
    created = brain.company_agent.create_invocation(
        envelope=envelope,
        goal="share artifact probe",
        trigger_kind="reactive",
        initial_message="share artifact probe",
    )
    return brain.store.get_task(created["id"])


def test_slack_send_artifact_dry_runs_with_no_bot_token_configured(brain):
    artifact = brain.artifacts.create(
        organization_id="org_acme",
        artifact_type="report",
        title="Export Failure Report",
        content="root cause: bad deploy",
        scope_id="scope_org_public",
    )
    destination = {"type": "internal_agent_session", "id": "artifact-dest", "is_private": True}
    run = _run(brain, destination)
    call = ModelToolCall(
        id="call_share",
        name="slack_send_artifact",
        arguments={"artifact_id": artifact["id"]},
    )
    result = brain.tool_executor.execute(run=run, call=call, approved=True)
    assert result.status == "completed"
    upload_result = result.output["upload_result"]
    assert upload_result["dry_run"] is True
    assert upload_result["ok"] is False
    assert upload_result["filename"] == "Export Failure Report.txt"


def test_slack_send_artifact_respects_scope_even_after_tool_call_is_approved(brain):
    private_artifact = brain.artifacts.create(
        organization_id="org_acme",
        artifact_type="report",
        title="Leadership Only Report",
        content="confidential",
        scope_id="scope_channel_leadership_private",
    )
    # A public-channel destination narrows allowed_scope_ids to exclude the
    # private scope, even though person_ajay is a member of it — the same
    # destination-narrowing rule the rest of the harness relies on
    # (services/authorization.py, exercised in tests/test_permissions.py).
    destination = {"type": "slack_channel", "id": "channel_eng", "is_private": False}
    run = _run(brain, destination)
    call = ModelToolCall(
        id="call_share_private",
        name="slack_send_artifact",
        arguments={"artifact_id": private_artifact["id"]},
    )
    result = brain.tool_executor.execute(run=run, call=call, approved=True)
    # The handler still re-checks scope at execution time and fails closed
    # after the policy decision.
    assert result.status == "failed"
    assert "scope" in (result.error or "").lower()


def test_slack_send_artifact_rejects_unknown_artifact_id(brain):
    destination = {"type": "internal_agent_session", "id": "artifact-missing", "is_private": True}
    run = _run(brain, destination)
    call = ModelToolCall(
        id="call_share_missing",
        name="slack_send_artifact",
        arguments={"artifact_id": "artifact_does_not_exist"},
    )
    result = brain.tool_executor.execute(run=run, call=call, approved=True)
    assert result.status == "failed"
    assert "not found" in (result.error or "").lower()
