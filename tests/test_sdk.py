"""SDK uses stable API paths and propagates caller payloads."""

from __future__ import annotations

import json

import httpx

from company_brain import CompanyBrain


def test_sdk_context_and_run_operations():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "path": request.url.path})

    with CompanyBrain(transport=httpx.MockTransport(handler)) as brain:
        context = brain.context.prepare(organization_id="org", task="Investigate", acting_principal_id="person")
        run = brain.runs.start(organization_id="org", task="Investigate", acting_principal_id="person", harness_id="h")
        brain.runs.record_action("run_1", event_type="action.completed", action="inspect")
        brain.runs.complete("run_1", outcome="resolved")
        brain.runs.evaluate("run_1")
        brain.events.publish(specversion="1.0", id="evt", source="h", type="task.completed", organizationid="org")
        brain.skills.search("org", "export")
        brain.context.resolve_owner(organization_id="org", issue_text="export stuck", acting_principal_id="person")
        brain.agent.invoke(
            organization_id="org",
            principal_id="person",
            conversation_id="api:test",
            destination={"type": "internal_agent_session", "id": "test", "is_private": True},
            goal="Investigate",
        )
        brain.agent.list("org")
        brain.approvals.list("org", status="pending")
        brain.harness.evaluation("org")
    assert context["path"] == "/v1/context:prepare"
    assert run["path"] == "/v1/runs"
    assert [request.url.path for request in requests] == [
        "/v1/context:prepare", "/v1/runs", "/v1/runs/run_1/events", "/v1/runs/run_1:complete", "/v1/runs/run_1:evaluate", "/v1/events", "/v1/skills:search", "/v1/owners:resolve", "/v1/agent/invoke", "/v1/agent/runs", "/v1/approvals", "/v1/harness/evaluation"
    ]
    assert json.loads(requests[0].content)["task"] == "Investigate"
