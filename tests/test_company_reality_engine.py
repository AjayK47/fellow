"""End-to-end requirements for the company reality engine and specialist workers."""

from __future__ import annotations

from docket.harness.models import (
    DestinationEnvelope,
    InvocationEnvelope,
    ModelToolCall,
)
from docket.services.policy import PolicyEngine


def _account(brain):
    account = {
        "id": "connector_test_linear",
        "organization_id": "org_acme",
        "toolkit": "linear",
        "connected_account_id": "ca_test_linear",
        "status": "active",
        "composio_user_id": "docket:org_acme",
        "principal_id": "connector:ca_test_linear",
        "scope_id": "scope_org_public",
        "learn": True,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    brain.store.upsert_connector_account(account)
    return account


def test_ingestion_materializes_object_identity_fact_and_case(brain):
    _account(brain)
    result = brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="linear",
        source_type="issue.updated",
        external_id="LIN-501",
        content="Issue: Docket status is investigating. Ajay owns the incident.",
        scope_id="scope_org_public",
        author_entity_id="person_ajay",
        entity_ids=["project_docket"],
        metadata={
            "object_type": "issue",
            "object_id": "LIN-501",
            "connected_account_id": "ca_test_linear",
            "raw_data": {
                "id": "LIN-501",
                "title": "Docket incident",
                "status": "investigating",
                "owner_id": "ajay-linear",
                "owner_email": "ajay@example.com",
                "owner_name": "Ajay",
            },
        },
        occurred_at="2026-03-04T05:06:07+00:00",
    )
    reality = result["reality"]
    assert reality["connector_object"]["external_id"] == "LIN-501"
    assert reality["connector_object"]["version"] == 1
    assert "person_ajay" in reality["entity_ids"]
    assert reality["facts"]
    assert result["source"]["occurred_at"] == "2026-03-04T05:06:07Z"
    assert all(
        fact["valid_from"] in {
            "2026-03-04T05:06:07Z",
            "2026-03-04T05:06:07+00:00",
        }
        for fact in reality["facts"]
    )
    assert reality["case"]["status"] == "open"

    identifier = brain.reality.lookup_identifier(
        "org_acme", "email", "AJAY@example.com"
    )
    assert identifier and identifier["entity_id"] == "person_ajay"


def test_connector_backfill_is_resumable_idempotent_and_learned(brain):
    account = _account(brain)
    run = brain.reality.begin_sync(
        organization_id="org_acme",
        connector_account_id=account["id"],
        object_type="issue",
    )
    result = brain._process_connector_backfill(
        {
            "sync_run_id": run["id"],
            "provider": "linear",
            "records": [
                {
                    "id": "LIN-900",
                    "title": "Export worker is failing",
                    "content": "Issue: export-worker is failing for Acme.",
                    "status": "open",
                }
            ],
            "next_cursor": "cursor-2",
        }
    )
    assert result["sync_run"]["status"] == "completed"
    assert result["sync_run"]["processed"] == 1
    cursor = brain.reality.get_sync_cursor(
        "org_acme", account["id"], "issue"
    )
    assert cursor["cursor"] == "cursor-2"

    # A replay produces a source version but not a duplicate canonical object.
    before = _connector_objects(brain, "LIN-900")
    run2 = brain.reality.begin_sync(
        organization_id="org_acme",
        connector_account_id=account["id"],
        object_type="issue",
    )
    brain._process_connector_backfill(
        {
            "sync_run_id": run2["id"],
            "provider": "linear",
            "records": [
                {
                    "id": "LIN-900",
                    "title": "Export worker is failing",
                    "content": "Issue: export-worker is failing for Acme.",
                    "status": "open",
                }
            ],
            "next_cursor": "cursor-2",
        }
    )
    assert len(_connector_objects(brain, "LIN-900")) == len(before)


def test_connector_backfill_paginates_remote_action_and_persists_cursor(brain):
    account = _account(brain)
    calls = []
    pages = [
        {
            "data": {
                "items": [{"id": "LIN-901", "title": "First historical issue"}],
                "next_cursor": "page-2",
            }
        },
        {
            "data": {
                "items": [{"id": "LIN-902", "title": "Second historical issue"}],
                "next_cursor": None,
            }
        },
    ]

    def execute(action_slug, arguments, *, connected_account_id):
        calls.append((action_slug, arguments, connected_account_id))
        return pages.pop(0)

    brain.composio.execute = execute
    run = brain.reality.begin_sync(
        organization_id="org_acme",
        connector_account_id=account["id"],
        object_type="issue",
    )
    result = brain._process_connector_backfill(
        {
            "sync_run_id": run["id"],
            "provider": "linear",
            "action_slug": "LINEAR_LIST_ISSUES",
            "action_arguments": {"team_id": "team-1"},
            "items_path": "data.items",
            "next_cursor_path": "data.next_cursor",
            "cursor_argument": "after",
            "max_pages": 10,
        }
    )
    assert result["pages"] == 2
    assert result["sync_run"]["processed"] == 2
    assert calls[0][1] == {"team_id": "team-1"}
    assert calls[1][1] == {"team_id": "team-1", "after": "page-2"}
    assert calls[0][2] == "ca_test_linear"
    assert {item["external_id"] for item in brain.reality.list_connector_objects(
        "org_acme", provider="linear", object_type="issue"
    )} >= {"LIN-901", "LIN-902"}


def _connector_objects(brain, external_id):
    with brain.store.cursor() as cur:
        cur.execute(
            "SELECT payload FROM connector_objects WHERE external_id = ?",
            (external_id,),
        )
        return cur.fetchall()


def test_bitemporal_facts_preserve_history_and_resolve_conflicts(brain):
    first = brain.reality.assert_fact(
        organization_id="org_acme",
        subject_id="project_docket",
        predicate="status",
        object_value="planning",
        scope_id="scope_org_public",
        valid_from="2026-01-01T00:00:00+00:00",
        source_ids=["source_old"],
        confidence=0.8,
        authority=0.7,
    )
    known_before = first["system_from"]
    second = brain.reality.assert_fact(
        organization_id="org_acme",
        subject_id="project_docket",
        predicate="status",
        object_value="active",
        scope_id="scope_org_public",
        valid_from="2026-02-01T00:00:00+00:00",
        source_ids=["source_new"],
        confidence=0.95,
        authority=0.9,
    )
    current = brain.reality.current_facts(
        "org_acme",
        subject_id="project_docket",
        predicate="status",
        valid_at="2026-03-01T00:00:00+00:00",
    )
    assert [fact["object_value"] for fact in current] == ["active"]
    historical = brain.reality.current_facts(
        "org_acme",
        subject_id="project_docket",
        predicate="status",
        valid_at="2026-01-15T00:00:00+00:00",
        known_at=known_before,
    )
    assert [fact["object_value"] for fact in historical] == ["planning"]
    conflicts = brain.reality.list_conflicts("org_acme")
    conflict = next(
        item for item in conflicts if second["id"] in item["fact_ids"]
    )
    assert conflict["kind"] == "mutually_exclusive"
    assert conflict["resolution"]["winner_fact_id"] == second["id"]


def test_cases_correlate_events_across_tools(brain):
    one = brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="slack",
        source_type="message",
        external_id="slack-case-1",
        content="Issue: Acme export-worker failures started today.",
        scope_id="scope_org_public",
        entity_ids=["customer_acme", "service_export_worker"],
    )
    two = brain.ingestion.ingest(
        organization_id="org_acme",
        source_system="github",
        source_type="issue",
        external_id="gh-case-1",
        content="Issue: export-worker is failing for Acme after deploy.",
        scope_id="scope_org_public",
        entity_ids=["customer_acme", "service_export_worker"],
    )
    assert one["reality"]["case"]["id"] == two["reality"]["case"]["id"]
    timeline = brain.reality.case_timeline(one["reality"]["case"]["id"])
    assert {
        one["source"]["id"],
        two["source"]["id"],
    }.issubset({event["source_id"] for event in timeline})
    updated = brain.reality.update_case(
        one["reality"]["case"]["id"],
        status="resolved",
        updated_by="person_ajay",
        summary="Retry storm mitigated.",
    )
    assert updated["status"] == "resolved"
    assert updated["status_history"][-1]["from"] == "open"


def test_judgments_become_retrievable_guidance(brain):
    item = brain.reality.record_judgment(
        organization_id="org_acme",
        judgment_type="agent_feedback",
        principal_id="person_ajay",
        subject={"run_id": "run-1"},
        decision="correction",
        reason="Always include the customer impact.",
    )
    guidance = brain.reality.judgment_guidance(
        "org_acme", "person_ajay"
    )
    assert item["id"] in {value["id"] for value in guidance}


def test_conditional_policy_default_deny_and_forbid_precedence(brain):
    brain.policy.create_version(
        organization_id="org_acme",
        policy_key="linear-writes",
        effect="permit",
        actions=["linear_update_issue"],
        conditions={"destination.type": "dm"},
        approval_required=True,
        created_by="person_ajay",
        status="active",
    )
    allowed = brain.policy.evaluate(
        organization_id="org_acme",
        action="linear_update_issue",
        context={"destination": {"type": "dm"}},
    )
    denied_by_default = brain.policy.evaluate(
        organization_id="org_acme",
        action="linear_update_issue",
        context={"destination": {"type": "slack_channel"}},
    )
    assert allowed["decision"] == "allow"
    assert allowed["approval_required"] is True
    assert denied_by_default["decision"] == "deny"

    brain.policy.create_version(
        organization_id="org_acme",
        policy_key="incident-freeze",
        effect="forbid",
        actions=["linear_update_issue"],
        conditions={"action.arguments.status": "closed"},
        created_by="person_ajay",
        status="active",
    )
    forbidden = brain.policy.evaluate(
        organization_id="org_acme",
        action="linear_update_issue",
        context={
            "destination": {"type": "dm"},
            "action": {"arguments": {"status": "closed"}},
        },
    )
    assert forbidden["decision"] == "deny"


def test_execution_preconditions_detect_state_change(brain):
    states = [{"id": "LIN-1", "version": 1}, {"id": "LIN-1", "version": 2}]

    def reader(**_kwargs):
        return states.pop(0)

    engine = PolicyEngine(brain.store, brain.audit, external_reader=reader)
    captured = engine.capture_preconditions(
        organization_id="org_acme",
        approval_id="approval-precondition",
        specifications=[
            {
                "action_slug": "LINEAR_GET_ISSUE",
                "arguments": {"issue_id": "$args.issue_id"},
            }
        ],
        runtime={},
        arguments={"issue_id": "LIN-1"},
    )
    assert captured
    check = engine.revalidate(
        approval_id="approval-precondition",
        runtime={},
        arguments={"issue_id": "LIN-1"},
    )
    assert check["valid"] is False
    assert check["changes"][0]["status"] == "changed"


def test_approved_tool_is_not_executed_when_precondition_changes(brain):
    _account(brain)
    states = [
        {"data": {"id": "LIN-1", "version": 1}},
        {"data": {"id": "LIN-1", "version": 2}},
    ]
    brain.policy.external_reader = lambda **_kwargs: states.pop(0)
    brain.policy.create_version(
        organization_id="org_acme",
        policy_key="linear-safe-update",
        effect="permit",
        actions=["composio_execute"],
        approval_required=True,
        precondition={
            "action_slug": "LINEAR_GET_ISSUE",
            "connected_account_id": "ca_test_linear",
            "arguments": {"issue_id": "$args.arguments.issue_id"},
        },
        created_by="person_ajay",
        status="active",
    )
    decision = brain.authz.resolve(
        organization_id="org_acme",
        acting_principal_id="person_ajay",
        destination={"type": "dm", "id": "person_ajay", "is_private": True},
        purpose="reactive_agent",
    )
    run = brain.company_agent.create_invocation(
        envelope=InvocationEnvelope(
            organization_id="org_acme",
            principal_id="person_ajay",
            conversation_id="precondition-run",
            destination=DestinationEnvelope(
                type="dm", id="person_ajay", is_private=True
            ),
            purpose="reactive_agent",
            permission_decision_id=decision.id,
            allowed_scope_ids=decision.allowed_scope_ids,
        ),
        goal="Update LIN-1",
        trigger_kind="reactive",
    )
    proposal = brain.tool_executor.execute(
        run=run,
        call=ModelToolCall(
            id="call-precondition",
            name="composio_execute",
            arguments={
                "tool_slug": "LINEAR_UPDATE_ISSUE",
                "arguments": {"issue_id": "LIN-1", "status": "closed"},
                "connected_account_id": "ca_test_linear",
            },
        ),
    )
    assert proposal.status == "waiting_for_approval"
    approval, result = brain.tool_executor.resolve_approval(
        approval_id=proposal.approval_id,
        approved=True,
        decided_by="person_ajay",
    )
    assert approval["status"] == "stale"
    assert result.status == "failed"
    assert "changed after approval" in result.error


def test_specialist_worker_is_child_read_only_and_returns_artifact(brain):
    decision = brain.authz.resolve(
        organization_id="org_acme",
        acting_principal_id="person_ajay",
        destination={"type": "dm", "id": "person_ajay", "is_private": True},
        purpose="reactive_agent",
    )
    parent = brain.company_agent.create_invocation(
        envelope=InvocationEnvelope(
            organization_id="org_acme",
            principal_id="person_ajay",
            conversation_id="specialist-parent",
            destination=DestinationEnvelope(
                type="dm", id="person_ajay", is_private=True
            ),
            purpose="reactive_agent",
            permission_decision_id=decision.id,
            allowed_scope_ids=decision.allowed_scope_ids,
        ),
        goal="Investigate export failures",
        trigger_kind="reactive",
        initial_message="Investigate export failures",
    )
    result = brain.specialists.execute(
        {
            "tasks": [
                {
                    "role": "incident-investigator",
                    "objective": "Find evidence about export-worker failures",
                    "allowed_tools": ["brain_search"],
                    "output_contract": "Findings with source IDs",
                    "max_iterations": 2,
                    "max_tool_calls": 3,
                }
            ]
        },
        {"run_id": parent["id"]},
    )
    worker = result["workers"][0]
    child = brain.agent_runs.get(worker["worker_run_id"])
    assert child["parent_run_id"] == parent["id"]
    assert child["metadata"]["specialist_worker"] is True
    assert "delegate_work" not in child["metadata"]["allowed_tools"]
    exposed = {
        definition.name
        for definition in brain.tool_executor.allowed_definitions(child)
    }
    assert exposed == {"brain_search"}
    forced_write = brain.tool_executor.execute(
        run=child,
        call=ModelToolCall(
            id="forced-specialist-write",
            name="brain_create_artifact",
            arguments={
                "artifact_type": "report",
                "title": "Should not exist",
                "content": {"unsafe": True},
                "scope_id": "scope_org_public",
            },
        ),
    )
    assert forced_write.status == "disabled"
    artifact = brain.artifacts.get(worker["artifact_id"])
    assert artifact["run_id"] == child["id"]
    assert artifact["metadata"]["parent_run_id"] == parent["id"]


def test_reality_api_exposes_permission_filtered_facts_and_artifacts(api_client):
    fact = api_client.post(
        "/v1/facts",
        json={
            "organization_id": "org_acme",
            "subject_id": "project_docket",
            "predicate": "status",
            "object_value": "active",
            "scope_id": "scope_org_public",
            "valid_from": "2026-01-01T00:00:00+00:00",
        },
    )
    assert fact.status_code == 200
    facts = api_client.get(
        "/v1/facts",
        params={
            "organization_id": "org_acme",
            "acting_principal_id": "person_ajay",
            "subject_id": "project_docket",
        },
    )
    assert facts.status_code == 200
    assert any(
        item["id"] == fact.json()["id"] for item in facts.json()["facts"]
    )

    artifact = api_client.post(
        "/v1/artifacts",
        json={
            "organization_id": "org_acme",
            "artifact_type": "report",
            "title": "Incident report",
            "content": {"summary": "Export worker recovered."},
            "scope_id": "scope_org_public",
            "created_by": "person_ajay",
        },
    )
    assert artifact.status_code == 200
    artifacts = api_client.get(
        "/v1/artifacts",
        params={
            "organization_id": "org_acme",
            "acting_principal_id": "person_ajay",
        },
    )
    assert artifacts.status_code == 200
    assert any(
        item["id"] == artifact.json()["id"]
        for item in artifacts.json()["artifacts"]
    )

    private_fact = api_client.post(
        "/v1/facts",
        json={
            "organization_id": "org_acme",
            "subject_id": "company",
            "predicate": "status",
            "object_value": "confidential",
            "scope_id": "scope_channel_leadership_private",
            "valid_from": "2026-01-01T00:00:00+00:00",
        },
    ).json()
    private_artifact = api_client.post(
        "/v1/artifacts",
        json={
            "organization_id": "org_acme",
            "artifact_type": "report",
            "title": "Leadership only",
            "content": {"summary": "Private"},
            "scope_id": "scope_channel_leadership_private",
            "created_by": "person_ajay",
        },
    ).json()
    ravi_facts = api_client.get(
        "/v1/facts",
        params={
            "organization_id": "org_acme",
            "acting_principal_id": "person_ravi",
        },
    ).json()["facts"]
    ravi_artifacts = api_client.get(
        "/v1/artifacts",
        params={
            "organization_id": "org_acme",
            "acting_principal_id": "person_ravi",
        },
    ).json()["artifacts"]
    assert private_fact["id"] not in {item["id"] for item in ravi_facts}
    assert private_artifact["id"] not in {item["id"] for item in ravi_artifacts}
    assert api_client.get(
        f"/v1/artifacts/{private_artifact['id']}",
        params={"acting_principal_id": "person_ravi"},
    ).status_code == 404


def test_policy_changes_are_pending_until_explicit_activation(api_client):
    created = api_client.post(
        "/v1/policies",
        json={
            "organization_id": "org_acme",
            "policy_key": "company-writes",
            "effect": "permit",
            "actions": ["linear_create_issue"],
            "conditions": {"destination.type": "dm"},
            "approval_required": True,
            "created_by": "person_ajay",
        },
    )
    assert created.status_code == 200
    assert created.json()["status"] == "pending"
    activated = api_client.post(
        f"/v1/policies/{created.json()['id']}:activate",
        json={"approved_by": "person_ajay"},
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == "active"


def test_tenant_deletion_covers_reality_and_artifact_tables(empty_brain):
    brain = empty_brain
    brain.store.put_organization({"id": "org_delete", "name": "Delete Me"})
    brain.store.upsert_scope(
        {
            "id": "scope_delete",
            "organization_id": "org_delete",
            "kind": "organization_public",
            "label": "Delete",
            "is_public": True,
        }
    )
    brain.reality.assert_fact(
        organization_id="org_delete",
        subject_id="thing",
        predicate="status",
        object_value="temporary",
        scope_id="scope_delete",
        valid_from="2026-01-01T00:00:00+00:00",
    )
    brain.artifacts.create(
        organization_id="org_delete",
        artifact_type="report",
        title="Temporary",
        content="delete",
        scope_id="scope_delete",
    )
    result = brain.retention.delete_organization(
        "org_delete", confirmation="org_delete"
    )
    assert result["deleted"]["reality_facts"] == 1
    assert result["deleted"]["artifacts"] == 1
