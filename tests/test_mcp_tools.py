"""MCP handler bindings cover reactive, procedural, execution, and proactive operations."""

from __future__ import annotations

from company_brain.mcp_tools import bind_mcp_handlers, mcp_tool_specs


def test_mcp_tools_cover_core_brain_operations(brain):
    specs = {spec["name"] for spec in mcp_tool_specs()}
    assert {"brain_search", "brain_prepare_context", "brain_search_skills", "brain_record_task_event", "brain_evaluate_intervention"}.issubset(specs)
    handlers = bind_mcp_handlers(brain)
    skills = handlers["brain_search_skills"](organization_id="org_acme", query="export")
    assert skills["results"]
    run = brain.runs.start(organization_id="org_acme", task="Investigate", acting_principal_id="person_ajay", harness_id="mcp")
    updated = handlers["brain_record_task_event"](run_id=run["id"], event_type="action.completed", action="inspect", result="success")
    assert updated["status"] == "running"
