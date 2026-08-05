"""MCP handler bindings cover reactive, procedural, execution, and proactive operations."""

from __future__ import annotations

from docket.harness.models import InvocationEnvelope
from docket.mcp_tools import bind_mcp_handlers, mcp_tool_specs


def test_mcp_tools_cover_core_brain_operations(brain):
    specs = {spec["name"] for spec in mcp_tool_specs()}
    assert {"brain_search", "brain_prepare_context", "brain_search_skills", "brain_record_task_event", "brain_evaluate_intervention"}.issubset(specs)
    handlers = bind_mcp_handlers(brain)
    skills = handlers["brain_search_skills"](organization_id="org_acme", query="export")
    assert skills["results"]
    destination = {"type": "internal_agent_session", "id": "mcp:task", "is_private": True}
    decision = brain.authz.resolve("org_acme", "person_ajay", destination, "reactive_agent")
    task = brain.company_agent.create_invocation(
        envelope=InvocationEnvelope(
            organization_id="org_acme",
            principal_id="person_ajay",
            conversation_id="mcp:task",
            destination=destination,
            purpose="reactive_agent",
            permission_decision_id=decision.id,
            allowed_scope_ids=decision.allowed_scope_ids,
        ),
        goal="Investigate",
        trigger_kind="reactive",
    )
    updated = handlers["brain_record_task_event"](
        task_id=task["id"], event_type="action.completed", action="inspect", result="success"
    )
    assert updated["id"] == task["id"]
    assert updated["events"][-1]["event_type"] == "action.completed"
