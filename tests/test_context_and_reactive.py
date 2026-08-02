"""Context package shape/budgeting and reactive operations."""

from __future__ import annotations


def test_prepare_context_structured_and_budgeted(brain):
    pkg = brain.context.prepare_context(
        organization_id="org_acme",
        task="Investigate the Acme export issue and launch date",
        acting_principal_id="person_ajay",
        destination={"type": "internal_agent_session", "id": "session_1", "is_private": True},
        purpose="incident_investigation",
        entity_ids=["customer_acme", "project_docket"],
        token_budget=500,
    )
    data = pkg.model_dump(mode="json")
    assert data["request_id"]
    assert data["permission_decision_id"]
    assert "current_facts" in data
    assert "decisions" in data
    assert "issues" in data
    assert "sources" in data
    assert "summary" in data
    assert data["token_budget"] == 500
    assert data["token_estimate"] <= 500
    # Should have real content from seed, not empty shell
    total_items = (
        len(data["current_facts"])
        + len(data["decisions"])
        + len(data["issues"])
        + len(data["open_commitments"])
    )
    assert total_items >= 1
    assert data["summary"]


def test_resolve_owner_includes_evidence(brain):
    result = brain.context.resolve_owner(
        organization_id="org_acme",
        issue_text="Acme exports are stuck",
        acting_principal_id="person_support_agent",
        entity_ids=["customer_acme", "service_export_worker"],
        include_on_call=True,
        destination={"type": "internal_agent_session", "id": "s", "is_private": True},
    )
    assert result["primary_owner"] is not None
    assert result["primary_owner"]["entity_id"] in {"person_ravi", "person_ajay"}
    assert result["evidence"], "owner resolution must include evidence"
    assert result["permission_decision_id"]
    # Ravi owns export-worker / Acme account
    assert result["on_call"] is None or result["on_call"]["entity_id"] == "person_meera"


def test_relationship_context_surfaces_shared_decisions(brain):
    result = brain.context.relationship_context(
        organization_id="org_acme",
        from_entity_id="person_ajay",
        to_entity_id="person_ravi",
        acting_principal_id="person_ajay",
        destination={"type": "internal_agent_session", "id": "s", "is_private": True},
    )
    assert result["graph_paths"] or result["shared_decisions"] or result["open_commitments"]
    assert result["permission_decision_id"]
    # Ajay and Ravi both WORKS_ON project_docket
    assert result["graph_paths"]


def test_project_context(brain):
    result = brain.context.project_context(
        organization_id="org_acme",
        project_id="project_docket",
        acting_principal_id="person_ajay",
        destination={"type": "internal_agent_session", "id": "s", "is_private": True},
    )
    assert result["project"]["id"] == "project_docket"
    assert any(m["id"] in {"person_ajay", "person_ravi"} for m in result["members"])
    assert result["permission_decision_id"]


def test_context_emits_audit(brain):
    brain.context.prepare_context(
        organization_id="org_acme",
        task="What is the launch date?",
        acting_principal_id="person_ajay",
        destination={"type": "internal_agent_session", "id": "s", "is_private": True},
        token_budget=2000,
    )
    events = brain.audit.list_events("org_acme", event_type="context.prepared")
    assert events
    assert events[-1]["payload"]["permission_decision_id"]
